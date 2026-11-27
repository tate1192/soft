# -*- coding: utf-8 -*-

import os
import datetime
import glob
import re
import string
import subprocess
import tempfile
import threading
import time
import json
import shutil
import stat
import struct
import zipfile
import logging

try:
    import odoo.release
    import odoo.sql_db
except ImportError:
    pass

from odoo import models, fields, api, tools
from odoo.exceptions import UserError, AccessDenied
from odoo.tools.misc import exec_pg_environ, find_pg_tool
from odoo.tools import SQL

_logger = logging.getLogger(__name__)

# Optional: cryptography for AES-256-GCM encryption
try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    CRYPTOGRAPHY_AVAILABLE = True
except ImportError:
    AESGCM = None
    CRYPTOGRAPHY_AVAILABLE = False


_ENCRYPTION_MAGIC = b'BVENC'
_ENCRYPTION_VERSION = 2
_ENCRYPTION_V2_PREFIX = _ENCRYPTION_MAGIC + bytes([_ENCRYPTION_VERSION])
_ENCRYPTION_CHUNK_SIZE = 16 * 1024 * 1024
_ENCRYPTION_NONCE_SIZE = 12
_ENCRYPTION_TAG_SIZE = 16
_ENCRYPTION_V2_HEADER = struct.Struct('<5sBIQ')
_ENCRYPTION_CHUNK_INDEX = struct.Struct('<Q')
_ENCRYPTION_AAD = struct.Struct('<IQII')
_PG_STDERR_LIMIT = 64 * 1024
_FILENAME_MAX_BYTES = 255


class BackupJob(models.Model):
    """
    Backup job model representing individual backup executions.
    
    This model tracks the execution of backup operations, including
    progress, results, and detailed logging.
    """
    _name = 'backup.job'
    _description = 'Backup Job'
    _order = 'start_time desc, id desc'
    _rec_name = 'display_name'
    
    # Basic information
    config_id = fields.Many2one(
        'backup.config',
        string='Backup Configuration',
        required=True,
        ondelete='cascade'
    )
    database_name = fields.Char(
        string='Database Name',
        required=True,
        help='Name of the database being backed up'
    )
    backup_format = fields.Selection([
        ('zip', 'ZIP Archive'),
        ('dump', 'PostgreSQL Dump'),
    ], string='Backup Format', required=True)
    is_manual = fields.Boolean(
        string='Manual Backup',
        default=False,
        help='True if this backup was triggered manually by a user'
    )
    
    # Status and timing
    status = fields.Selection([
        ('pending', 'Pending'),
        ('running', 'Running'),
        ('success', 'Success'),
        ('warning', 'Warning'),
        ('error', 'Error'),
    ], string='Status', required=True, default='pending')
    start_time = fields.Datetime(
        string='Start Time',
        required=True,
        default=fields.Datetime.now
    )
    end_time = fields.Datetime(
        string='End Time'
    )
    duration = fields.Float(
        string='Duration (seconds)',
        compute='_compute_duration',
        store=True,
        help='Total backup duration in seconds'
    )
    duration_human = fields.Char(
        string='Duration',
        compute='_compute_duration_human',
        help='Total backup duration in a human-readable format (e.g. "1h 2m 15s")'
    )

    # File information
    backup_filename = fields.Char(
        string='Backup Filename',
        help='Name of the generated backup file'
    )
    backup_size_mb = fields.Float(
        string='Backup Size (MB)',
        digits=(20, 6),
        help='Size of the backup file in megabytes (with sub-MB precision)'
    )
    backup_size_human = fields.Char(
        string='Backup Size',
        compute='_compute_backup_size_human',
        help='Human-readable backup file size'
    )
    
    # Results and logging
    backup_details = fields.Text(
        string='Backup Details',
        help='Detailed information about the backup process'
    )
    error_message = fields.Text(
        string='Error Message',
        help='Error message if backup failed'
    )
    log_entries = fields.Text(
        string='Log Entries',
        help='Detailed log of backup process'
    )
    
    # Provider results
    provider_results = fields.Text(
        string='Provider Results',
        help='JSON-encoded results from storage providers'
    )
    
    # Verification
    verification_status = fields.Selection([
        ('not_verified', 'Not Verified'),
        ('verified', 'Verified'),
        ('failed', 'Verification Failed'),
    ], string='Verification Status', default='not_verified')
    verification_details = fields.Text(
        string='Verification Details'
    )
    
    # Computed fields
    display_name = fields.Char(
        string='Display Name',
        compute='_compute_display_name',
        store=True
    )
    
    @api.depends('config_id.name', 'start_time', 'status')
    def _compute_display_name(self):
        """Compute display name for backup job."""
        for record in self:
            if record.config_id and record.start_time:
                record.display_name = f"{record.config_id.name} - {record.start_time.strftime('%Y-%m-%d %H:%M')}"
            else:
                record.display_name = f"Backup Job #{record.id}"
    
    @api.depends('start_time', 'end_time')
    def _compute_duration(self):
        """Compute backup duration."""
        for record in self:
            if record.start_time and record.end_time:
                delta = record.end_time - record.start_time
                record.duration = delta.total_seconds()
            else:
                record.duration = 0.0

    @api.depends('duration')
    def _compute_duration_human(self):
        """Format duration as a compact human-readable string (e.g. "1h 2m 15s")."""
        for record in self:
            total = int(record.duration or 0)
            if total <= 0:
                record.duration_human = '—'
                continue
            hours, remainder = divmod(total, 3600)
            minutes, seconds = divmod(remainder, 60)
            parts = []
            if hours:
                parts.append(f"{hours}h")
            if minutes:
                parts.append(f"{minutes}m")
            if seconds or not parts:
                parts.append(f"{seconds}s")
            record.duration_human = ' '.join(parts)

    @api.depends('backup_size_mb')
    def _compute_backup_size_human(self):
        """Compute human-readable backup size (auto-scales to KB / MB / GB)."""
        for record in self:
            if record.backup_size_mb and record.backup_size_mb > 0:
                size_bytes = int(record.backup_size_mb * 1024 * 1024)
                record.backup_size_human = tools.human_size(size_bytes) or ''
            else:
                record.backup_size_human = ''
    
    # Prefixes used for every temp dir this module creates under /tmp. The
    # safety sweep keys off these to avoid touching anything else.
    _TEMP_DIR_PREFIXES = ('odoo_backup_', 'odoo_backup_dump_', 'backup_restore_')

    # Marker file written inside every temp dir with the creating process
    # PID, so the stale sweep can tell an in-progress backup (owner process
    # alive) from the leftover of a killed worker (owner process gone).
    _TEMP_OWNER_MARKER = '.odoo_backup_owner'

    def _process_backup(self):
        """
        Process the backup job.

        This is the main method that orchestrates the entire backup process.
        Owns the lifecycle of the temporary directory end-to-end so the outer
        finally block can always clean it up, no matter where (or how) a
        nested step fails.
        """
        self.ensure_one()

        # Safety net: remove leftover temp dirs from previous runs that were
        # killed (SIGKILL/OOM/restart) before their own finally could run.
        # Runs before we allocate this run's temp dir so a wedged /tmp from
        # an earlier failure gets a chance to free space.
        self._cleanup_stale_temp_files()

        self._log("Starting backup process")

        # Durability: commit the freshly created job row now. Everything below
        # can run for many minutes inside this transaction; if the worker dies
        # mid-backup the row (and later the watchdog below) must still exist.
        self._commit_progress()

        # Allocate the temp dir up front so the outer finally always owns it.
        # Previously this was created inside _create_backup_file, so if that
        # method raised before returning, the outer finally saw temp_dir=None
        # and only the inner except (with ignore_errors=True) ran — silently
        # swallowing any cleanup failure.
        temp_dir = tempfile.mkdtemp(prefix='odoo_backup_')
        self._mark_temp_dir_owner(temp_dir)
        try:
            # Validate and generate the filename inside the job lifecycle so a
            # bad template is recorded as a failed job and the temp dir is
            # still cleaned up.
            self.backup_filename = self._generate_backup_filename()

            # Fail fast if the configured database is not even on this server
            # (a configuration carried over from another database keeps the
            # original name), then if a provider cannot receive the file.
            # Dumping first and only then discovering a missing source database
            # or an unwritable directory wastes minutes and gigabytes.
            self._preflight_database()
            self._preflight_providers()

            # Create backup file inside our temp dir
            backup_file_path, filestore_problems = self._create_backup_file(temp_dir)

            # Get backup file size and store as MB with sub-MB precision (Float)
            size_bytes = os.path.getsize(backup_file_path)
            self.backup_size_mb = size_bytes / (1024 * 1024)
            self._log(f"Backup file created: {backup_file_path} ({tools.human_size(size_bytes)})")

            # Verify backup integrity if enabled
            if self.config_id.verify_backups:
                self._verify_backup_integrity(backup_file_path)

            # Upload to storage providers
            provider_results = self._upload_to_providers(backup_file_path)
            self.provider_results = self._serialize_provider_results(provider_results)

            # Determine final status
            successful_uploads = sum(1 for r in provider_results.values() if r.get('success'))
            total_providers = len(self.config_id.all_providers)

            if successful_uploads == 0:
                # All uploads failed
                self.status = 'error'
                self.error_message = "All storage provider uploads failed"
                success = False
            elif successful_uploads < total_providers:
                # Some uploads failed
                self.status = 'warning'
                self.error_message = f"Only {successful_uploads}/{total_providers} provider uploads succeeded"
                success = True  # Partial success
            elif filestore_problems:
                # The archive is complete for every file the filestore still
                # holds, but the database references files that are not there.
                # Say so loudly: a warning still sends the failure notification
                # and keeps retention from pruning the last good copies.
                problems = filestore_problems
                self.status = 'warning'
                self.error_message = (
                    'Backup stored with a partial filestore: %d missing file(s), '
                    '%d size mismatch(es), %d unusable metadata row(s)'
                    % (
                        len(problems.get('missing_files') or []),
                        len(problems.get('size_mismatch') or []),
                        len(problems.get('invalid_metadata') or []),
                    )
                )
                success = True
            else:
                # All uploads succeeded
                self.status = 'success'
                success = True

            self.end_time = fields.Datetime.now()
            self._log(f"Backup process completed with status: {self.status}")
            self._commit_progress()

            return {
                'success': success,
                'message': self.error_message or 'Backup completed successfully',
                'backup_job': self,
                'provider_results': provider_results
            }
        except Exception as e:
            error_message = self._format_backup_error(e)
            self.status = 'error'
            self.end_time = fields.Datetime.now()
            self.error_message = error_message
            self._log(f"Backup process failed: {error_message}")
            self._commit_progress()

            return {
                'success': False,
                'message': error_message,
                'backup_job': self
            }
        finally:
            # Always clean up the temporary directory and its contents.
            # Uses _safe_rmtree so a failure to remove is logged (not silently
            # swallowed by ignore_errors=True) but never masks the original
            # exception by re-raising.
            self._safe_rmtree(temp_dir, label='backup temp dir')

    def _commit_progress(self):
        """Commit the job row so progress survives a killed worker.

        The whole backup runs in a single transaction; without an explicit
        commit a SIGKILL (cron ``limit_time_cpu``, OOM killer, service
        restart) rolls the job record back and the attempt becomes invisible:
        no row in the UI and no failure e-mail.
        """
        # Skipped in test mode (TransactionCase depends on the rollback).
        # Everything is inside the try so that even a broken flag lookup can
        # never take a backup down with it.
        try:
            if tools.config['test_enable']:
                return
            self.env.cr.commit()
        except Exception as e:  # a failed commit must never kill the backup
            _logger.warning("Could not commit backup job progress: %s", e)
    
    def _generate_backup_filename(self):
        """Generate a unique, filesystem-safe filename from the template."""
        template = self.config_id.backup_name_template
        if template is None or template is False:
            template = '{database}_{timestamp}_{job}.{format}'
        if not isinstance(template, str) or not template:
            raise UserError('Backup name template must not be empty.')
        if '\x00' in template:
            raise UserError('Backup name template must not contain NUL bytes.')

        timestamp = datetime.datetime.now().strftime('%Y_%m_%d_%H_%M_%S_%f')
        job_id = self.id if isinstance(self.id, int) and self.id > 0 else 'new'
        variables = {
            'database': self.database_name,
            'timestamp': timestamp,
            'format': self.backup_format,
            'config': self.config_id.name,
            'job': str(job_id),
        }

        try:
            parts = list(string.Formatter().parse(template))
        except ValueError as e:
            raise UserError('Invalid backup name template: %s' % e)

        field_names = set()
        for _literal, field_name, format_spec, conversion in parts:
            if field_name is None:
                continue
            if not field_name or field_name not in variables:
                raise UserError(
                    'Unknown backup name template variable {%s}. '
                    'Available variables: {database}, {timestamp}, {job}, '
                    '{format}, {config}.' % field_name
                )
            if format_spec or conversion:
                raise UserError(
                    'Backup name template variables do not support format '
                    'specifiers or conversions.'
                )
            field_names.add(field_name)

        filename = template.format(**variables)

        # Older configurations use the former default template without {job}.
        # Add the uniqueness components when omitted so retries and concurrent
        # jobs cannot overwrite one another at a storage provider.
        suffix = []
        if 'timestamp' not in field_names:
            suffix.append(timestamp)
        if 'job' not in field_names:
            suffix.append(str(job_id))
        if suffix:
            stem, extension = os.path.splitext(filename)
            filename = '%s_%s%s' % (stem, '_'.join(suffix), extension)

        return self._sanitize_filename(filename)

    def _sanitize_filename(self, filename):
        """Sanitize and validate a single backup filename."""
        if not isinstance(filename, str) or not filename:
            raise UserError('Backup filename must not be empty.')
        if '\x00' in filename:
            raise UserError('Backup filename must not contain NUL bytes.')
        if any(ord(char) < 32 or ord(char) == 127 for char in filename):
            raise UserError('Backup filename must not contain control characters.')

        # Remove or replace invalid characters. Path separators are replaced,
        # never stripped, so the result is always a single path component.
        filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
        filename = filename.strip().strip('_')
        if not filename or filename in ('.', '..'):
            raise UserError('Backup filename must not be empty or a dot path.')
        if len(filename.encode('utf-8')) > _FILENAME_MAX_BYTES:
            raise UserError(
                'Backup filename must not exceed %d bytes.' % _FILENAME_MAX_BYTES
            )
        return filename
    
    def _create_backup_file(self, temp_dir):
        """Create the actual backup file inside `temp_dir`.

        The caller owns `temp_dir` and is responsible for its cleanup (see
        _process_backup), so this method does not create or remove it.
        """
        self._log(f"Creating {self.backup_format.upper()} backup of database: {self.database_name}")

        backup_file_path = os.path.join(temp_dir, self.backup_filename)
        filestore_problems = {}
        try:
            with open(backup_file_path, 'wb') as backup_file:
                filestore_problems = self._create_database_dump(backup_file) or {}

            self._log(f"Backup file created successfully: {backup_file_path}")
            return backup_file_path, filestore_problems
        except Exception as e:
            raise UserError(f"Failed to create backup file: {self._format_backup_error(e)}")

    @staticmethod
    def _format_backup_error(e):
        """Produce a concise, human-readable message for backup errors.

        `shutil.copytree` raises `shutil.Error` with a list of per-file
        (src, dst, why) tuples — stringifying it dumps the whole list into
        the email body. Detect common root causes (disk full, permissions)
        and fall back to a length-capped string for everything else.
        """
        msg = str(e)
        # Messages we raise ourselves (e.g. the provider pre-flight check) are
        # already written for the operator and name the field to fix; never
        # rewrite them into a generic hint.
        if isinstance(e, UserError):
            max_len = 500
            return msg if len(msg) <= max_len else msg[:max_len] + ' … (message truncated)'
        if 'No space left on device' in msg or 'Errno 28' in msg:
            return ("No space left on device while writing temporary files. "
                    "Free up disk space (typically on /tmp) and retry.")
        if 'Permission denied' in msg or 'Errno 13' in msg:
            return ("Permission denied while writing temporary files. "
                    "Check that the Odoo user can write to /tmp and to the "
                    "configured local backup paths.")
        # Generic fallback: cap length so the email stays readable
        max_len = 500
        if len(msg) > max_len:
            return msg[:max_len] + ' … (message truncated)'
        return msg
    
    def _create_database_dump(self, stream):
        """
        Create database dump.

        Uses an internal dump implementation that replicates Odoo's
        ``odoo.service.db.dump_db`` logic without the
        ``@check_db_management_enabled`` decorator.  This allows backups
        to work even when ``list_db = False`` is set in odoo.conf.

        Security note: because this bypasses ``@check_db_management_enabled``,
        a full database dump can be produced even when the administrator has
        disabled web database management via ``list_db = False``. Authorization
        is therefore enforced here against backup-admin group membership (see
        the check below) rather than relying on Odoo's db-management gate.
        """
        # Authorization check: a database dump is a full export of the data and
        # must be restricted to the backup system (cron) or a backup
        # administrator. We verify group membership explicitly here instead of
        # trusting ``is_manual``, which is a caller-controlled context flag
        # (defaults to True) and provides no real authorization. ``is_manual``
        # is kept purely as audit metadata on the job.
        cron_user = self.env.ref('backup_versatile.backup_cron').user_id
        is_cron_user = self.env.user.id == cron_user.id
        is_backup_admin = self.env.user.has_group('backup_versatile.group_backup_admin')

        if not is_cron_user and not is_backup_admin:
            raise AccessDenied("Database dumps require backup administrator rights")

        self._log(f"Creating {self.backup_format} dump of database: {self.database_name}")

        try:
            problems = self._dump_db(self.database_name, stream, self.backup_format) or {}
            self._log("Database dump created successfully")
            return problems
        except Exception as e:
            self._log(f"Database dump failed: {str(e)}")
            raise UserError(f"Database backup failed: {str(e)}")

    # ------------------------------------------------------------------
    # Internal dump helpers (mirror odoo.service.db without decorator)
    # ------------------------------------------------------------------

    def _dump_db_manifest(self, cr):
        """Generate the manifest dict for a ZIP backup.

        Replicates ``odoo.service.db.dump_db_manifest`` so that we are
        not affected by the ``@check_db_management_enabled`` decorator.
        """
        pg_version = "%d.%d" % divmod(cr._obj.connection.server_version / 100, 100)
        cr.execute("SELECT name, latest_version FROM ir_module_module WHERE state = 'installed'")
        modules = dict(cr.fetchall())
        return {
            'odoo_dump': '1',
            'db_name': cr.dbname,
            'version': odoo.release.version,
            'version_info': odoo.release.version_info,
            'major_version': odoo.release.major_version,
            'pg_version': pg_version,
            'modules': modules,
        }

    @staticmethod
    def _validate_filestore_path(path):
        """Validate a database filestore path before using it in a ZIP."""
        if not isinstance(path, str) or not path:
            raise UserError('Invalid empty filestore path in attachment metadata.')
        if '\x00' in path or '\\' in path or ':' in path:
            raise UserError('Invalid filestore path in attachment metadata: %r' % path)
        if path.startswith('/') or any(part in ('', '.', '..') for part in path.split('/')):
            raise UserError('Invalid filestore path in attachment metadata: %r' % path)
        return path

    def _get_filestore_manifest(self, cr):
        """Return the filestore files referenced by the source database.

        A missing filestore directory is valid only when the database has no
        file-backed attachments.  The database metadata is therefore the
        authority for deciding whether an absent or unmounted filestore is a
        legitimate empty one or an incomplete backup source.
        """
        cr.execute(SQL(
            "SELECT store_fname, file_size FROM ir_attachment "
            "WHERE type = 'binary' AND store_fname IS NOT NULL "
            "AND store_fname <> '' ORDER BY store_fname"
        ))
        expected = {}
        invalid = []
        for store_fname, file_size in cr.fetchall():
            try:
                path = self._validate_filestore_path(store_fname)
            except UserError:
                # A single corrupt metadata row (an external-storage style
                # store_fname such as ``bucket://xx/yy.pdf``, say) must not make
                # the whole database unbackupable.  Record it and keep
                # archiving everything the metadata does describe.
                invalid.append(str(store_fname))
                continue
            if not isinstance(file_size, int) or isinstance(file_size, bool) or file_size < 0:
                invalid.append(path)
                continue
            if path in expected and expected[path] != file_size:
                invalid.append(path)
                continue
            expected[path] = file_size

        return {
            'file_count': len(expected),
            'total_size': sum(expected.values()),
            'files': [
                {'path': path, 'size': expected[path]}
                for path in sorted(expected)
            ],
            'invalid_metadata': sorted(invalid),
        }

    @classmethod
    def _parse_filestore_manifest(cls, value):
        """Validate manifest filestore metadata and return path -> size."""
        if not isinstance(value, dict):
            raise UserError('Invalid filestore metadata in backup manifest.')
        files = value.get('files')
        file_count = value.get('file_count')
        total_size = value.get('total_size')
        if not isinstance(files, list):
            raise UserError('Backup manifest is missing filestore files metadata.')
        if not isinstance(file_count, int) or isinstance(file_count, bool) or file_count < 0:
            raise UserError('Invalid filestore file count in backup manifest.')
        if not isinstance(total_size, int) or isinstance(total_size, bool) or total_size < 0:
            raise UserError('Invalid filestore total size in backup manifest.')

        expected = {}
        for entry in files:
            if not isinstance(entry, dict):
                raise UserError('Invalid filestore file entry in backup manifest.')
            path = cls._validate_filestore_path(entry.get('path'))
            size = entry.get('size')
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                raise UserError('Invalid filestore file size in backup manifest.')
            if path in expected:
                raise UserError('Duplicate filestore path in backup manifest: %s' % path)
            expected[path] = size

        if file_count != len(expected):
            raise UserError('Filestore file count does not match backup manifest entries.')
        if total_size != sum(expected.values()):
            raise UserError('Filestore total size does not match backup manifest entries.')
        return expected

    def _dump_db(self, db_name, stream, backup_format='zip'):
        """Dump *db_name* into the file-like *stream*.

        This is a faithful copy of ``odoo.service.db.dump_db`` **without**
        the ``@check_db_management_enabled`` decorator so that backups
        work regardless of the ``list_db`` setting.
        """
        _logger.info(
            'DUMP DB: %s format %s with filestore', db_name, backup_format,
        )

        cmd = [find_pg_tool('pg_dump'), '--no-owner', db_name]
        env = exec_pg_environ()

        if backup_format == 'zip':
            # Manual mkdtemp + try/finally instead of `with TemporaryDirectory()`:
            # if rmtree fails (e.g. disk full mid-write leaves partial state),
            # TemporaryDirectory.__exit__ swallows the rmtree error and leaks
            # the directory under /tmp. _safe_rmtree logs the failure reason,
            # then retries with ignore_errors so we still clean up what we can.
            #
            # The temp dir holds only dump.sql + manifest.json (small). The
            # filestore is streamed straight into the archive from its live
            # location by _write_backup_zip — we do NOT copy it here first.
            # Odoo's stock dump_db copies the filestore into the temp dir before
            # zipping, which makes it exist twice on disk at peak (a full copy
            # under /tmp *and* inside the growing zip). On a shared/contended
            # disk that doubled footprint is exactly what runs /tmp out of space.
            dump_dir = tempfile.mkdtemp(prefix='odoo_backup_dump_')
            self._mark_temp_dir_owner(dump_dir)
            try:
                # Generate manifest, including the database's authoritative
                # list of file-backed attachments.  The ZIP writer uses this
                # to distinguish an empty filestore from a missing mount and to
                # fail closed if any referenced file disappears.
                with open(os.path.join(dump_dir, 'manifest.json'), 'w') as fh:
                    db = odoo.sql_db.db_connect(db_name)
                    with db.cursor() as cr:
                        manifest = self._dump_db_manifest(cr)
                        manifest['filestore'] = self._get_filestore_manifest(cr)
                        json.dump(manifest, fh, indent=4)

                # Run pg_dump -> dump.sql (kept in the temp dir)
                cmd.insert(-1, '--file=' + os.path.join(dump_dir, 'dump.sql'))
                subprocess.run(
                    cmd, env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.STDOUT,
                    check=True,
                )

                # Build the zip directly into the output stream
                return self._write_backup_zip(stream, dump_dir, db_name)
            finally:
                self._safe_rmtree(dump_dir, label='dump_dir')
        else:
            cmd.insert(-1, '--format=c')
            # Stream stdout directly into the caller's file.  Capturing the
            # whole custom dump with stdout=PIPE would require a second copy
            # of a potentially multi-gigabyte backup in worker memory.
            self._stream_pg_dump(cmd, env, stream)

    def _stream_pg_dump(self, cmd, env, stream):
        """Stream a custom pg_dump to *stream* with bounded stderr memory."""
        process = subprocess.Popen(
            cmd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stderr = bytearray()

        def read_stderr():
            # Drain the pipe continuously so pg_dump cannot deadlock when its
            # diagnostics exceed the OS pipe buffer.  Keep only a bounded
            # tail for the eventual error message.
            while True:
                chunk = process.stderr.read(4096)
                if not chunk:
                    break
                stderr.extend(chunk)
                if len(stderr) > _PG_STDERR_LIMIT:
                    del stderr[:-_PG_STDERR_LIMIT]

        reader = threading.Thread(
            target=read_stderr, name='backup-pg-dump-stderr', daemon=True,
        )
        reader.start()
        try:
            shutil.copyfileobj(process.stdout, stream, length=1024 * 1024)
        except Exception:
            # A failed output stream must not leave pg_dump blocked on a full
            # pipe.  Preserve the original write error after reaping it.
            try:
                process.kill()
            except OSError:
                pass
            process.wait()
            raise
        finally:
            process.stdout.close()
            reader.join()
            process.stderr.close()

        returncode = process.wait()
        if returncode:
            detail = stderr.decode('utf-8', errors='replace').strip()
            if detail:
                raise UserError(
                    'pg_dump failed with exit code %d: %s'
                    % (returncode, detail)
                )
            raise UserError('pg_dump failed with exit code %d.' % returncode)

    def _write_backup_zip(self, stream, dump_dir, db_name):
        """Write a restore-compatible backup zip into *stream*.

        Layout matches Odoo's ``dump_db`` so the archive restores through the
        standard path: ``dump.sql`` is first (so a restore can read it without
        scanning the whole archive), followed by the filestore and finally the
        manifest.  Unlike ``dump_db`` the filestore is read from its live
        location instead of a temp copy.

        Compression strategy: dump.sql and manifest.json are DEFLATEd (text,
        compresses well). Filestore entries are STOREd: attachments are
        already-compressed binaries (images, PDFs, ...) where DEFLATE costs a
        lot of CPU for negligible size gain — enough that on a large filestore
        the cron worker burns through its ``limit_time_cpu`` (default 600s)
        mid-archive and gets SIGKILL'd before any commit, leaving no job
        record and no failure email. zipfile reads both methods transparently
        on restore, so this is purely a cost-side change.

        allowZip64 is mandatory: real filestores routinely exceed the 4 GiB
        ZIP32 limit.
        """
        filestore = odoo.tools.config.filestore(db_name)
        if os.path.islink(filestore):
            raise UserError('Filestore root is an unsupported symbolic link: %s' % filestore)
        with open(os.path.join(dump_dir, 'manifest.json')) as manifest_file:
            manifest = json.load(manifest_file)
        filestore_metadata = manifest.get('filestore')
        expected = (
            self._parse_filestore_manifest(filestore_metadata)
            if filestore_metadata is not None else None
        )

        with zipfile.ZipFile(
            stream, 'w', compression=zipfile.ZIP_STORED, allowZip64=True,
        ) as zf:
            # dump.sql must remain first for Odoo's restore fast path.  The
            # manifest is written after the filestore walk so it describes
            # exactly the entries that were archived, including harmless
            # orphan files left behind by attachment garbage collection.
            zf.write(
                os.path.join(dump_dir, 'dump.sql'), 'dump.sql',
                compress_type=zipfile.ZIP_DEFLATED,
            )

            if expected and not os.path.isdir(filestore):
                raise UserError(
                    'Filestore directory is missing or unmounted, but the '
                    'database references %d filestore file(s): %s'
                    % (len(expected), filestore)
                )

            archived = {}
            if os.path.exists(filestore):

                def raise_walk_error(error):
                    # os.walk otherwise suppresses permission and I/O errors,
                    # turning an unreadable subtree into a silently partial ZIP.
                    raise error

                for dirpath, dirnames, filenames in os.walk(
                    filestore, onerror=raise_walk_error,
                ):
                    dirnames.sort()
                    for fname in sorted(filenames):
                        fpath = os.path.join(dirpath, fname)
                        relative = os.path.relpath(fpath, filestore).replace(os.sep, '/')
                        self._validate_filestore_path(relative)
                        if os.path.islink(fpath):
                            raise UserError(
                                'Filestore contains an unsupported symbolic link: %s'
                                % fpath
                            )
                        file_stat = os.stat(fpath)
                        if not stat.S_ISREG(file_stat.st_mode):
                            raise UserError('Filestore entry is not a regular file: %s' % fpath)
                        arcname = 'filestore/' + relative
                        try:
                            zf.write(fpath, arcname)
                        except FileNotFoundError:
                            # Removed between the walk and the read (attachment
                            # GC).  The manifest names it as a missing file and
                            # the job ends as ``warning`` instead of losing the
                            # whole backup to one vanished attachment.
                            continue
                        archived[relative] = file_stat.st_size

            problems = {}
            database_filestore_metadata = filestore_metadata or {}
            if expected is not None:
                # A missing attachment file is the database's own condition (GC
                # orphans, an attachment whose bytes never landed), not a reason
                # to hand the customer no backup at all: archive what exists,
                # list the rest in the manifest and let the job finish as
                # ``warning``.  Only an absent/unmounted filestore root (handled
                # above) and structural errors (symlink, unreadable tree, walk
                # error) still fail the backup.
                missing = sorted(set(expected) - set(archived))
                wrong_size = sorted(
                    path for path in expected
                    if path in archived and archived[path] != expected[path]
                )
                invalid_metadata = list(database_filestore_metadata.get('invalid_metadata') or [])
                if missing:
                    problems['missing_files'] = missing
                if wrong_size:
                    problems['size_mismatch'] = wrong_size
                if invalid_metadata:
                    problems['invalid_metadata'] = invalid_metadata
                if problems:
                    self._log(
                        'Filestore archived partially: %d missing, %d size mismatch, '
                        '%d unusable metadata row(s); backup continues as warning'
                        % (len(missing), len(wrong_size), len(invalid_metadata))
                    )

            filestore_metadata = {
                'file_count': len(archived),
                'total_size': sum(archived.values()),
                'files': [
                    {'path': path, 'size': archived[path]}
                    for path in sorted(archived)
                ],
            }
            if expected is not None:
                filestore_metadata['database_file_count'] = len(expected)
                filestore_metadata['database_total_size'] = sum(expected.values())
                filestore_metadata.update(problems)
            manifest['filestore'] = filestore_metadata
            manifest_path = os.path.join(dump_dir, 'manifest.json')
            with open(manifest_path, 'w') as manifest_file:
                json.dump(manifest, manifest_file, indent=4)
            zf.write(
                manifest_path, 'manifest.json',
                compress_type=zipfile.ZIP_DEFLATED,
            )
        return problems
    
    def _verify_backup_integrity(self, backup_file_path):
        """Verify backup file integrity."""
        self._log("Verifying backup integrity")
        
        try:
            # Basic file checks
            if not os.path.exists(backup_file_path):
                raise UserError("Backup file does not exist")
            
            file_size = os.path.getsize(backup_file_path)
            if file_size == 0:
                raise UserError("Backup file is empty")
            
            # Format-specific verification
            if self.backup_format == 'zip':
                self._verify_zip_backup(backup_file_path)
            else:
                self._verify_dump_backup(backup_file_path)
            
            self.verification_status = 'verified'
            self.verification_details = f"Backup verified successfully. File size: {tools.human_size(file_size)}"
            self._log("Backup integrity verification passed")
        except Exception as e:
            self.verification_status = 'failed'
            self.verification_details = f"Verification failed: {e}"
            self._log(f"Backup integrity verification failed: {e}")
            raise UserError(f"Backup verification failed: {e}")
    
    def _verify_zip_backup(self, backup_file_path):
        """Verify ZIP backup integrity."""
        try:
            with zipfile.ZipFile(backup_file_path, 'r') as zip_file:
                # Test ZIP file integrity — testzip() returns the name of the
                # first corrupt entry, or None when all CRCs are valid.
                bad_file = zip_file.testzip()
                if bad_file:
                    raise UserError("Corrupt entry in ZIP backup: %s" % bad_file)
                
                # Check required files
                file_list = zip_file.namelist()
                if 'dump.sql' not in file_list:
                    raise UserError("ZIP backup missing dump.sql file")
                
                if 'manifest.json' not in file_list:
                    raise UserError("ZIP backup missing manifest.json file")
                
                # Validate manifest
                with zip_file.open('manifest.json') as manifest_file:
                    manifest = json.loads(manifest_file.read().decode())
                    if manifest.get('db_name') != self.database_name:
                        raise UserError("Manifest database name mismatch")

                filestore_metadata = manifest.get('filestore')
                if filestore_metadata is not None:
                    expected = self._parse_filestore_manifest(filestore_metadata)
                    archived = {}
                    for info in zip_file.infolist():
                        if not info.filename.startswith('filestore/') or info.is_dir():
                            continue
                        path = self._validate_filestore_path(
                            info.filename[len('filestore/'):]
                        )
                        if path in archived:
                            raise UserError('Duplicate filestore entry in ZIP: %s' % path)
                        archived[path] = info.file_size

                    if len(archived) != filestore_metadata['file_count']:
                        raise UserError(
                            'ZIP filestore count mismatch: expected %d, found %d'
                            % (filestore_metadata['file_count'], len(archived))
                        )
                    missing = sorted(set(expected) - set(archived))
                    if missing:
                        raise UserError(
                            'ZIP backup is missing expected filestore file(s): %s'
                            % ', '.join(missing[:10])
                        )
                    unexpected = sorted(set(archived) - set(expected))
                    if unexpected:
                        raise UserError(
                            'ZIP backup contains unexpected filestore file(s): %s'
                            % ', '.join(unexpected[:10])
                        )
                    wrong_size = [
                        path for path in expected
                        if archived[path] != expected[path]
                    ]
                    if wrong_size:
                        raise UserError(
                            'ZIP filestore file size mismatch for: %s'
                            % ', '.join(sorted(wrong_size)[:10])
                        )
        except zipfile.BadZipFile:
            raise UserError("Backup file is not a valid ZIP archive")
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise UserError("Invalid manifest in ZIP backup: %s" % e)
    
    def _verify_dump_backup(self, backup_file_path):
        """Verify a PostgreSQL custom dump with ``pg_restore --list``."""
        try:
            with open(backup_file_path, 'rb') as f:
                header = f.read(5)
                # PostgreSQL custom format dumps start with 'PGDMP'.  This is
                # only a cheap pre-check; pg_restore below validates the
                # archive structure and table of contents.
                if header != b'PGDMP':
                    raise UserError("File is not a valid PostgreSQL dump")

            with tempfile.TemporaryFile() as stderr_file:
                result = subprocess.run(
                    [find_pg_tool('pg_restore'), '--list', backup_file_path],
                    env=exec_pg_environ(),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=stderr_file,
                    check=False,
                )
                if result.returncode:
                    stderr_file.seek(0, os.SEEK_END)
                    size = stderr_file.tell()
                    stderr_file.seek(max(0, size - _PG_STDERR_LIMIT))
                    detail = stderr_file.read(_PG_STDERR_LIMIT).decode(
                        'utf-8', errors='replace',
                    ).strip()
                    raise UserError(
                        'pg_restore --list failed with exit code %d%s'
                        % (
                            result.returncode,
                            ': %s' % detail if detail else '.',
                        )
                    )
        except UserError:
            raise
        except Exception as e:
            raise UserError("Failed to verify dump file: %s" % e)
    
    def _preflight_database(self):
        """Raise before the dump when the configured database is missing.

        ``database_name`` is a plain stored field on the configuration: a
        configuration created on one database and then carried over to another
        (a restored copy, a staging clone, a test instance) keeps the original
        name.  Every run then died in the middle of the dump with a bare
        ``FATAL: database "x" does not exist`` — *after* the storage providers
        had been probed and reported as ready, which reads as if the provider
        were the problem.

        ``pg_database`` is cluster-wide, so the current cursor answers the
        question for any database name on this server: exactly the server the
        dump connects to.
        """
        self.ensure_one()

        self.env.cr.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s",
            (self.database_name,))
        if self.env.cr.fetchone():
            self._log(
                "Pre-flight check passed for database '%s'"
                % self.database_name)
            return True

        hint = ''
        served = self.env.cr.dbname
        if self.database_name != served:
            hint = (" This configuration was created on another database — "
                    "this instance serves '%s'." % served)
        raise UserError(
            "Database '%s' does not exist on this server.%s Update the "
            "'Database Name' field on the backup configuration."
            % (self.database_name, hint))

    def _preflight_providers(self):
        """Raise before the dump if any configured provider is not usable.

        Every failure caught here is one the operator can fix in seconds,
        while the same failure *after* the dump costs the whole dump.
        Raises UserError with the provider name and the concrete reason.
        """
        self.ensure_one()

        for provider in self.config_id.all_providers:
            result = provider._preflight_check()
            if not result.get('success'):
                raise UserError(
                    "Storage provider '%s' is not ready: %s"
                    % (provider.name, result.get('message') or 'unknown reason')
                )
            self._log(
                "Pre-flight check passed for provider '%s' -> %s"
                % (provider.name, result.get('message') or 'ready')
            )

        return True

    def _upload_to_providers(self, backup_file_path):
        """Upload backup to all configured storage providers."""
        providers = self.config_id.all_providers

        # Encrypt before upload if enabled
        if self.config_id.encrypt_backup:
            backup_file_path = self._encrypt_backup_file(backup_file_path)

        return self._upload_to_providers_sequential(backup_file_path, providers)

    @staticmethod
    def _decode_encryption_key(key_raw):
        """Decode and validate a base64-encoded AES-256 key."""
        import base64
        try:
            key = base64.b64decode(key_raw, validate=True)
        except Exception as e:
            raise UserError('Invalid encryption key (not valid base64): %s' % e)
        if len(key) != 32:
            raise UserError(
                'Encryption key must be exactly 32 bytes '
                '(got %d bytes after base64 decode).' % len(key)
            )
        return key

    @classmethod
    def _validate_encryption_chunk_size(cls, chunk_size):
        if not 1 <= chunk_size <= _ENCRYPTION_CHUNK_SIZE:
            raise UserError(
                'Invalid encrypted chunk size %d; expected a value between '
                '1 and %d bytes.' % (chunk_size, _ENCRYPTION_CHUNK_SIZE)
            )

    def _encrypt_backup_file(self, backup_file_path):
        """Encrypt the backup file with authenticated AES-256-GCM chunks.

        Version 2 layout::

            [5B magic "BVENC"][1B version=2][4B LE chunk_size][8B LE chunks]
            [repeat: 8B LE index][12B nonce][4B LE ct_len][N B ciphertext]

        The AAD binds the format prefix, chunk size, chunk index, total chunk
        count and final-chunk flag.  Reordering, duplicating, removing or
        appending chunks therefore fails authentication instead of producing a
        plausible but incomplete plaintext file.
        """
        if not CRYPTOGRAPHY_AVAILABLE:
            raise UserError(
                'Encryption requires the ``cryptography`` package.\n'
                'Install it with: pip install cryptography'
            )

        key_raw = self.config_id.encrypt_key
        if not key_raw:
            raise UserError(
                'Encryption is enabled but no encryption key is set. '
                'Go to the backup configuration and click '
                '"Generate Encryption Key".'
            )

        key = self._decode_encryption_key(key_raw)
        aesgcm = AESGCM(key)
        chunk_size = _ENCRYPTION_CHUNK_SIZE
        enc_path = backup_file_path + '.enc'
        output_dir = os.path.dirname(os.path.abspath(enc_path))
        fd, temp_path = tempfile.mkstemp(prefix='.bv_encrypt_', dir=output_dir)

        self._log('Encrypting backup with AES-256-GCM')
        try:
            with os.fdopen(fd, 'wb') as fout:
                fd = None
                with open(backup_file_path, 'rb') as fin:
                    source_size = os.fstat(fin.fileno()).st_size
                    bytes_read = 0
                    total_chunks = max(
                        1, (source_size + chunk_size - 1) // chunk_size,
                    )
                    fout.write(_ENCRYPTION_V2_HEADER.pack(
                        _ENCRYPTION_MAGIC,
                        _ENCRYPTION_VERSION,
                        chunk_size,
                        total_chunks,
                    ))

                    for index in range(total_chunks):
                        chunk = fin.read(chunk_size)
                        bytes_read += len(chunk)
                        final = index == total_chunks - 1
                        if not final and len(chunk) != chunk_size:
                            raise UserError('Backup file changed while encrypting.')
                        nonce = os.urandom(_ENCRYPTION_NONCE_SIZE)
                        aad = _ENCRYPTION_V2_PREFIX + _ENCRYPTION_AAD.pack(
                            index, chunk_size, total_chunks, int(final),
                        )
                        ciphertext = aesgcm.encrypt(nonce, chunk, aad)
                        fout.write(_ENCRYPTION_CHUNK_INDEX.pack(index))
                        fout.write(nonce)
                        fout.write(len(ciphertext).to_bytes(4, 'little'))
                        fout.write(ciphertext)

                    if fin.read(1):
                        raise UserError('Backup file changed while encrypting.')
                    if bytes_read != source_size:
                        raise UserError('Backup file changed while encrypting.')
                fout.flush()
                os.fsync(fout.fileno())
            os.replace(temp_path, enc_path)
        except Exception as e:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if os.path.exists(temp_path):
                os.unlink(temp_path)
            if isinstance(e, UserError):
                raise
            raise UserError('Encryption failed: %s' % e)

        self._log('Encryption complete: %s' % enc_path)
        return enc_path

    @staticmethod
    def _decrypt_v2(fin, fout, aesgcm):
        """Decrypt and strictly validate a version-2 encrypted stream."""
        header = fin.read(_ENCRYPTION_V2_HEADER.size)
        if len(header) != _ENCRYPTION_V2_HEADER.size:
            raise UserError('Truncated encrypted file header.')
        magic, version, chunk_size, total_chunks = _ENCRYPTION_V2_HEADER.unpack(header)
        if magic != _ENCRYPTION_MAGIC or version != _ENCRYPTION_VERSION:
            raise UserError('Unsupported encrypted backup format.')
        BackupJob._validate_encryption_chunk_size(chunk_size)
        if total_chunks < 1:
            raise UserError('Encrypted backup has an invalid chunk count.')

        file_size = os.fstat(fin.fileno()).st_size
        min_chunk_wire_size = (
            _ENCRYPTION_CHUNK_INDEX.size
            + _ENCRYPTION_NONCE_SIZE
            + 4
            + _ENCRYPTION_TAG_SIZE
        )
        max_chunk_wire_size = min_chunk_wire_size + chunk_size
        minimum_size = _ENCRYPTION_V2_HEADER.size + total_chunks * min_chunk_wire_size
        maximum_size = _ENCRYPTION_V2_HEADER.size + total_chunks * max_chunk_wire_size
        if not minimum_size <= file_size <= maximum_size:
            raise UserError('Encrypted backup length does not match its chunk count.')

        for expected_index in range(total_chunks):
            index_bytes = fin.read(_ENCRYPTION_CHUNK_INDEX.size)
            if len(index_bytes) != _ENCRYPTION_CHUNK_INDEX.size:
                raise UserError('Truncated chunk index in encrypted file.')
            index = _ENCRYPTION_CHUNK_INDEX.unpack(index_bytes)[0]
            if index != expected_index:
                raise UserError(
                    'Encrypted backup chunk order mismatch: expected %d, found %d.'
                    % (expected_index, index)
                )

            nonce = fin.read(_ENCRYPTION_NONCE_SIZE)
            if len(nonce) != _ENCRYPTION_NONCE_SIZE:
                raise UserError('Truncated nonce in encrypted file.')
            ct_len_bytes = fin.read(4)
            if len(ct_len_bytes) != 4:
                raise UserError('Truncated ciphertext length.')
            ct_len = int.from_bytes(ct_len_bytes, 'little')
            final = index == total_chunks - 1
            if ct_len < _ENCRYPTION_TAG_SIZE or ct_len > chunk_size + _ENCRYPTION_TAG_SIZE:
                raise UserError('Invalid ciphertext length in encrypted file.')
            if not final and ct_len != chunk_size + _ENCRYPTION_TAG_SIZE:
                raise UserError('Non-final encrypted chunk has an invalid length.')

            ciphertext = fin.read(ct_len)
            if len(ciphertext) != ct_len:
                raise UserError('Truncated ciphertext.')
            aad = _ENCRYPTION_V2_PREFIX + _ENCRYPTION_AAD.pack(
                index, chunk_size, total_chunks, int(final),
            )
            plaintext = aesgcm.decrypt(nonce, ciphertext, aad)
            if not final and len(plaintext) != chunk_size:
                raise UserError('Non-final encrypted chunk has an invalid plaintext size.')
            fout.write(plaintext)

        if fin.read(1):
            raise UserError('Trailing data after the final encrypted chunk.')

    @staticmethod
    def _decrypt_v1(fin, fout, aesgcm):
        """Decrypt the legacy unauthenticated chunk format when possible."""
        chunk_size_bytes = fin.read(4)
        if len(chunk_size_bytes) != 4:
            raise UserError('Truncated encrypted file.')
        chunk_size = int.from_bytes(chunk_size_bytes, 'little')
        BackupJob._validate_encryption_chunk_size(chunk_size)

        while True:
            nonce = fin.read(_ENCRYPTION_NONCE_SIZE)
            if not nonce:
                break
            if len(nonce) != _ENCRYPTION_NONCE_SIZE:
                raise UserError('Truncated nonce in encrypted file.')
            ct_len_bytes = fin.read(4)
            if len(ct_len_bytes) != 4:
                raise UserError('Truncated ciphertext length.')
            ct_len = int.from_bytes(ct_len_bytes, 'little')
            if not _ENCRYPTION_TAG_SIZE <= ct_len <= chunk_size + _ENCRYPTION_TAG_SIZE:
                raise UserError('Invalid ciphertext length in encrypted file.')
            ciphertext = fin.read(ct_len)
            if len(ciphertext) != ct_len:
                raise UserError('Truncated ciphertext.')
            fout.write(aesgcm.decrypt(nonce, ciphertext, None))

    @staticmethod
    def decrypt_backup_file(enc_path, output_path, key_raw):
        """Decrypt a backup into *output_path* atomically.

        Version 2 is fully authenticated.  The legacy version-1 format remains
        readable for existing backups, but cannot provide chunk reordering or
        truncation detection because it did not authenticate chunk metadata.
        A failed decryption never replaces or leaves a partial output file.
        """
        if not CRYPTOGRAPHY_AVAILABLE:
            raise UserError('Decryption requires ``cryptography`` package.')

        key = BackupJob._decode_encryption_key(key_raw)
        aesgcm = AESGCM(key)
        output_dir = os.path.dirname(os.path.abspath(output_path))
        fd, temp_path = tempfile.mkstemp(prefix='.bv_decrypt_', dir=output_dir)

        try:
            with os.fdopen(fd, 'wb') as fout:
                fd = None
                with open(enc_path, 'rb') as fin:
                    prefix = fin.read(len(_ENCRYPTION_V2_PREFIX))
                    fin.seek(0)
                    if prefix == _ENCRYPTION_V2_PREFIX:
                        BackupJob._decrypt_v2(fin, fout, aesgcm)
                    elif prefix.startswith(_ENCRYPTION_MAGIC):
                        raise UserError('Unsupported encrypted backup version.')
                    else:
                        BackupJob._decrypt_v1(fin, fout, aesgcm)
                fout.flush()
                os.fsync(fout.fileno())
            os.replace(temp_path, output_path)
        except Exception:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if os.path.exists(temp_path):
                os.unlink(temp_path)
            raise

        return output_path
    
    def _upload_to_providers_sequential(self, backup_file_path, providers):
        """Upload to providers sequentially."""
        results = {}
        
        for provider in providers:
            destination = provider._get_destination_path()
            label = "%s -> %s" % (provider.name, destination) if destination else provider.name
            result_key = "%s:%s" % (provider._name, provider.id)
            self._log(f"Uploading to provider: {label}")
            
            try:
                result = provider.upload_backup(backup_file_path, self.backup_filename)
                results[result_key] = result
                
                if result.get('success'):
                    self._log(f"Upload to {label} successful")
                else:
                    self._log(f"Upload to {label} failed: {result.get('message', 'Unknown error')}")
            except Exception as e:
                error_msg = f"Upload to {label} failed with exception: {e}"
                self._log(error_msg)
                results[result_key] = {
                    'success': False,
                    'message': str(e),
                    'metadata': {}
                }
        
        return results
    
    def _serialize_provider_results(self, provider_results):
        """Serialize provider results to JSON, handling datetime objects."""
        def datetime_serializer(obj):
            """JSON serializer function for datetime objects."""
            if isinstance(obj, datetime.datetime):
                return obj.isoformat()
            elif isinstance(obj, datetime.date):
                return obj.isoformat()
            raise TypeError(f"Object of type {type(obj)} is not JSON serializable")
        
        try:
            return json.dumps(provider_results, indent=2, default=datetime_serializer)
        except Exception as e:
            # Fallback - convert to string representation
            _logger.warning("Failed to serialize provider results to JSON: %s", str(e))
            return str(provider_results)
    
    def _log(self, message):
        """Add entry to backup job log."""
        timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        log_entry = f"[{timestamp}] {message}"
        
        if self.log_entries:
            self.log_entries += f"\n{log_entry}"
        else:
            self.log_entries = log_entry

        # Cap the stored log: log_entries is an unpruned Text column and _log
        # runs on every step, so a chatty backup must not grow the row forever.
        max_log_length = 200000
        if len(self.log_entries) > max_log_length:
            self.log_entries = (
                '[log truncated]\n' + self.log_entries[-max_log_length:]
            )
        
        # Also log to system logger. Use %s (not %d) so this also works on
        # non-persisted records (.new()), where self.id is a NewId.
        _logger.info("Backup Job %s: %s", self.id or 'new', message)

    @staticmethod
    def _safe_rmtree(path, label='temp dir'):
        """Remove a directory tree, logging the reason if it can't be fully
        removed, then retrying with ignore_errors=True so we never leave
        behind a partially-cleaned path *and* never mask the original
        exception by re-raising from a finally block.
        """
        if not path or not os.path.exists(path):
            return
        try:
            shutil.rmtree(path)
        except Exception as e:
            _logger.warning(
                "Could not fully remove %s %s: %s — retrying with ignore_errors",
                label, path, e,
            )
            shutil.rmtree(path, ignore_errors=True)
            if os.path.exists(path):
                _logger.error("Leaked %s: %s still exists after cleanup", label, path)

    @classmethod
    def _mark_temp_dir_owner(cls, path):
        """Write the creating process PID into the temp dir (owner marker).

        The stale sweep uses this to protect backups that are legitimately
        still running — however long they take — while still reclaiming
        dirs whose owner process is gone (SIGKILL, OOM, restart).
        """
        try:
            with open(os.path.join(path, cls._TEMP_OWNER_MARKER), 'w') as fh:
                fh.write(str(os.getpid()))
        except OSError as e:
            # Never fail a backup over the marker; the sweep falls back to
            # age-based cleanup for dirs without one.
            _logger.warning("Could not write owner marker in %s: %s", path, e)

    @staticmethod
    def _pid_is_alive(pid):
        """Return True if a process with this PID currently exists."""
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # exists but owned by another user
        except OSError:
            return False
        return True

    @classmethod
    def _temp_dir_owner_alive(cls, path):
        """Return True if the temp dir's owner marker names a live process."""
        try:
            with open(os.path.join(path, cls._TEMP_OWNER_MARKER)) as fh:
                pid = int(fh.read().strip())
        except (OSError, ValueError):
            # No/unreadable marker (e.g. dir created by a pre-marker version
            # of this module): fall back to pure age-based cleanup.
            return False
        return cls._pid_is_alive(pid)

    @classmethod
    def _cleanup_stale_temp_files(cls, max_age_seconds=2 * 3600,
                                  hard_max_age_seconds=24 * 3600):
        """Sweep the system temp dir for leftover backup temp dirs and remove
        the ones that no longer belong to a running backup.

        This is the safety net for cases the per-run try/finally can't cover:
        the worker getting SIGKILL'd by the OOM killer, an Odoo restart in
        the middle of a long filestore copy, or shutil.rmtree silently
        failing under disk pressure with ignore_errors=True.

        A dir is removed only when ALL of these hold:
        - it is older than ``max_age_seconds``, and
        - its owner marker (the PID of the process that created it) does not
          name a live process — so a backup that legitimately runs for many
          hours is never swept out from under the worker writing to it.

        ``hard_max_age_seconds`` is retained for caller compatibility, but it
        never overrides a live owner marker.  Deleting an active worker's files
        is less acceptable than retaining an abandoned directory whose PID has
        been recycled by an unrelated process.
        """
        tmp_root = tempfile.gettempdir()
        now = time.time()
        cutoff = now - max_age_seconds
        removed = []
        for prefix in cls._TEMP_DIR_PREFIXES:
            for path in glob.glob(os.path.join(tmp_root, prefix + '*')):
                try:
                    mtime = os.path.getmtime(path)
                    if mtime >= cutoff:
                        continue  # recent — likely an in-progress backup
                    if os.path.isdir(path) and cls._temp_dir_owner_alive(path):
                        continue  # never delete a live owner's directory
                    if os.path.isdir(path):
                        shutil.rmtree(path, ignore_errors=True)
                    else:
                        os.unlink(path)
                    if not os.path.exists(path):
                        removed.append(path)
                except Exception as e:
                    _logger.warning(
                        "Could not remove stale backup temp path %s: %s", path, e,
                    )
        if removed:
            _logger.info(
                "Backup temp sweep removed %d stale path(s): %s",
                len(removed), ', '.join(removed),
            )

    @api.model
    def _cleanup_stuck_jobs(self, max_age_hours=6, batch_limit=500):
        """Mark backups left 'running'/'pending' long past their start as errors.

        ``_commit_progress`` makes an in-flight job visible even when the
        worker is killed, but nothing ever closes such a row: the UI would show
        a backup that never ends and the failure would never be reported.
        Called from the daily cleanup cron (backup.config.cleanup_old_backups).
        """
        cutoff = fields.Datetime.now() - datetime.timedelta(hours=max_age_hours)
        stuck = self.search([
            ('status', 'in', ('running', 'pending')),
            ('start_time', '<', cutoff),
        ], limit=batch_limit)
        for job in stuck:
            job.write({
                'status': 'error',
                'end_time': fields.Datetime.now(),
                'error_message': 'Backup was interrupted: the worker stopped '
                                 'before the backup could finish. Check the '
                                 'system log and retry.',
            })
            job._log(
                'Marked as failed: no completion within %d hour(s).' % max_age_hours
            )
        if stuck:
            _logger.warning(
                "Marked %d interrupted backup job(s) as failed", len(stuck)
            )
        return len(stuck)

    def _assert_backup_admin(self):
        """Reject backup execution for read-only users."""
        if not self.env.user.has_group('backup_versatile.group_backup_admin'):
            raise AccessDenied(self.env._(
                'Backup administrator rights are required for this operation.'
            ))

    def retry_backup(self):
        """Retry a failed backup job."""
        self._assert_backup_admin()
        self.ensure_one()
        
        if self.status not in ['error', 'warning']:
            raise UserError("Can only retry failed or warning backups")
        
        # Reset job status
        self.write({
            'status': 'pending',
            'end_time': False,
            'error_message': False,
            'log_entries': False,
            'provider_results': False,
            'verification_status': 'not_verified',
            'verification_details': False,
        })
        
        # Process the backup
        return self._process_backup()
    
    def view_provider_results(self):
        """View detailed provider results."""
        self.ensure_one()
        
        if not self.provider_results:
            raise UserError("No provider results available")
        
        try:
            results = json.loads(self.provider_results)
            formatted_results = json.dumps(results, indent=2)
        except:
            formatted_results = self.provider_results
        
        return {
            'type': 'ir.actions.act_window',
            'name': 'Provider Results',
            'res_model': 'backup.job',
            'view_mode': 'form',
            'res_id': self.id,
            'target': 'new',
            'context': {
                'default_provider_results': formatted_results,
            },
        }
