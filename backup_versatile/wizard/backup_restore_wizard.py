# -*- coding: utf-8 -*-

import json
import logging
import os
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import uuid
import zipfile
from datetime import datetime

import odoo
import odoo.api
import odoo.modules.neutralize
import odoo.modules.registry
import odoo.sql_db

from odoo import models, fields, api, _
from odoo.exceptions import AccessDenied, UserError
from odoo.tools import config
from odoo.tools.misc import exec_pg_environ, find_pg_tool

_logger = logging.getLogger(__name__)


class BackupRestoreWizard(models.TransientModel):
    """Wizard to restore a backup from any configured provider.

    The downloaded archive is fully validated before the target is touched.  A
    fresh staging database is restored and verified, then exchanged with the
    target using renames.  Both the database and filestore exchanges keep the
    previous target around until the whole cutover succeeds so an exception can
    roll the operation back.
    """

    _name = 'backup.restore.wizard'
    _description = 'Restore Backup Wizard'

    _DB_NAME_RE = re.compile(r'^[a-zA-Z0-9_][a-zA-Z0-9_.-]*$')
    _MAX_DB_NAME_BYTES = 63
    _MAX_ZIP_MEMBERS = 100000
    _MAX_ZIP_MEMBER_BYTES = 20 * 1024 ** 3
    _MAX_ZIP_EXTRACTED_BYTES = 200 * 1024 ** 3
    _MAX_ZIP_MEMBER_NAME_BYTES = 1024
    _MAX_MANIFEST_BYTES = 1024 * 1024
    _RESTORE_TIMEOUT = 3600

    # ── Step 1: Provider selection ──────────────────────────────────────

    provider_type = fields.Selection(
        selection=[
            ('local', 'Local Provider'),
            ('sftp', 'SFTP Provider'),
            ('cloud', 'Cloud Provider'),
        ],
        string='Storage Type',
        required=True,
        help='Type of storage provider to restore from',
    )

    local_provider_id = fields.Many2one(
        'backup.provider.local',
        string='Local Provider',
        domain="[('active', '=', True)]",
    )
    sftp_provider_id = fields.Many2one(
        'backup.provider.sftp',
        string='SFTP Provider',
        domain="[('active', '=', True)]",
    )
    cloud_provider_id = fields.Many2one(
        'backup.provider.cloud',
        string='Cloud Provider',
        domain="[('active', '=', True)]",
    )

    # ── Step 2: Backup selection ────────────────────────────────────────

    available_backups = fields.Text(
        string='Available Backups',
        readonly=True,
        help='Backup files found on the selected provider. '
             'Refresh the list after selecting a provider.',
    )
    selected_backup = fields.Char(
        string='Backup Filename',
        required=True,
        help='Name of the backup file to restore',
    )

    # ── Step 3: Restore options ─────────────────────────────────────────

    target_database = fields.Char(
        string='Target Database Name',
        required=True,
        help='Name of the database to restore into. '
             'Will be created if it does not exist.',
    )
    create_database = fields.Boolean(
        string='Create Database',
        default=True,
        help='Create the target database if it does not exist',
    )
    neutralize_database = fields.Boolean(
        string='Neutralize Database',
        default=True,
        help='After restore, disable outgoing emails, crons, and payment '
             'providers — safe for staging/development copies.',
    )
    drop_existing = fields.Boolean(
        string='Drop Existing Database',
        default=False,
        help='Replace an existing target only after a staged restore has '
             'succeeded. The previous database is retained for rollback.',
    )

    # ── Encryption (if the backup was encrypted) ────────────────────────

    backup_is_encrypted = fields.Boolean(
        string='Encrypted Backup',
        default=False,
        help='Check this if the backup was encrypted with AES-256-GCM',
    )
    encryption_key = fields.Char(
        string='Encryption Key (base64)',
        groups='backup_versatile.group_backup_admin',
        copy=False,
        help='The AES-256 key used when the backup was created '
             '(base64-encoded, 32 bytes)',
    )

    # ── State ───────────────────────────────────────────────────────────

    state = fields.Selection(
        selection=[
            ('select', 'Select Provider & Backup'),
            ('confirm', 'Confirm Restore'),
            ('restoring', 'Restoring...'),
            ('done', 'Restore Complete'),
        ],
        default='select',
        required=True,
        readonly=True,
    )
    restore_log = fields.Text(
        string='Restore Log',
        readonly=True,
    )

    # ── Provider selection helpers ──────────────────────────────────────

    def _get_provider(self):
        """Return the selected provider record."""
        self.ensure_one()
        if self.provider_type == 'local' and self.local_provider_id:
            return self.local_provider_id
        elif self.provider_type == 'sftp' and self.sftp_provider_id:
            return self.sftp_provider_id
        elif self.provider_type == 'cloud' and self.cloud_provider_id:
            return self.cloud_provider_id
        raise UserError(_('Please select a provider first.'))

    @classmethod
    def _validate_database_name(cls, name):
        """Validate a PostgreSQL identifier before using it in a command."""
        if (
            not isinstance(name, str)
            or not cls._DB_NAME_RE.fullmatch(name)
            or len(name.encode('utf-8')) > cls._MAX_DB_NAME_BYTES
        ):
            raise UserError(_(
                'Invalid database name. Use 1-63 bytes consisting only of '
                'letters, numbers, underscores, hyphens, and dots, starting '
                'with a letter, number, or underscore.'
            ))
        return name

    def _assert_system_admin(self):
        """Restore is a server-wide operation, not a per-database privilege."""
        if not self.env.user.has_group('base.group_system'):
            raise AccessDenied(
                "Database restore requires system administrator rights."
            )

    def _validate_restore_options(self):
        """Reject option combinations that would destroy data."""
        self.ensure_one()
        if not self.create_database:
            raise UserError(_(
                'Restoring requires "Create Database" because the backup is '
                'first restored into a fresh staging database.'
            ))
        if self.target_database in ('postgres', 'template0', 'template1'):
            raise UserError(_(
                'The target database "%s" is reserved by PostgreSQL. Choose a '
                'different target database name.'
            ) % self.target_database)
        # Never target the database this instance is serving: the restore
        # would drop/overwrite the registry executing this very code, and the
        # filestore swap below would remove its attachments as well.
        if self.target_database == self.env.cr.dbname:
            raise UserError(_(
                'The target database "%s" is the database this Odoo instance '
                'is currently running on. Restoring on top of it would destroy '
                'the live database. Choose a different target database name.'
            ) % self.target_database)

    @staticmethod
    def _sanitize_backup_filename(filename):
        """Return a safe basename for the backup file, rejecting traversal."""
        if not filename:
            return filename
        if '\x00' in filename:
            raise UserError(_('Invalid backup filename: null byte not allowed.'))
        safe = os.path.basename(filename.rstrip('/'))
        if safe in ('', '.', '..'):
            raise UserError(_(
                'Invalid backup filename. Provide a plain file name (no folders).'
            ))
        return safe

    # ── Archive validation and extraction ───────────────────────────────

    @staticmethod
    def _quote_pg_identifier(name):
        """Quote an already validated PostgreSQL identifier."""
        return '"%s"' % name.replace('"', '""')

    @staticmethod
    def _quote_pg_literal(value):
        """Quote an already validated PostgreSQL string literal."""
        return "'%s'" % value.replace("'", "''")

    @classmethod
    def _scratch_database_name(cls, role):
        """Return a collision-resistant name below PostgreSQL's 63-byte limit."""
        return '_bv_%s_%s' % (role, uuid.uuid4().hex)

    @staticmethod
    def _zip_member_parts(name):
        """Validate and split a ZIP member name without normalizing traversal."""
        if not isinstance(name, str) or not name or '\x00' in name:
            raise UserError(_('Invalid empty or null-containing ZIP member name.'))
        if len(name.encode('utf-8')) > BackupRestoreWizard._MAX_ZIP_MEMBER_NAME_BYTES:
            raise UserError(_('ZIP member name is too long: %s') % name)
        if '\\' in name or name.startswith('/') or re.match(r'^[A-Za-z]:', name):
            raise UserError(_('Refusing unsafe ZIP member path: %s') % name)

        path = name[:-1] if name.endswith('/') else name
        parts = path.split('/')
        if any(part in ('', '.', '..') for part in parts):
            raise UserError(_('Refusing unsafe ZIP member path: %s') % name)
        if parts in (['dump.sql'], ['manifest.json']):
            return parts
        if parts == ['filestore'] and name.endswith('/'):
            return parts
        if len(parts) >= 2 and parts[0] == 'filestore':
            return parts
        raise UserError(_('Unexpected member in backup ZIP: %s') % name)

    @staticmethod
    def _validate_zip_info(info):
        """Reject ZIP symlinks and special files before extracting anything."""
        mode = (info.external_attr >> 16) & 0xFFFF
        file_type = stat.S_IFMT(mode)
        if info.is_dir():
            if file_type not in (0, stat.S_IFDIR):
                raise UserError(_('Unsafe directory entry in backup ZIP: %s') % info.filename)
            return
        if file_type not in (0, stat.S_IFREG):
            raise UserError(_('Refusing non-regular ZIP member: %s') % info.filename)

    def _validate_zip_archive(self, zf):
        """Validate structure, limits, manifest and every member's CRC."""
        try:
            infos = zf.infolist()
            if len(infos) > self._MAX_ZIP_MEMBERS:
                raise UserError(_(
                    'Backup ZIP has too many members (maximum %s).'
                ) % self._MAX_ZIP_MEMBERS)

            by_name = {}
            total_size = 0
            for info in infos:
                self._zip_member_parts(info.filename)
                self._validate_zip_info(info)
                if info.filename in by_name:
                    raise UserError(_('Duplicate member in backup ZIP: %s') % info.filename)
                by_name[info.filename] = info
                if info.file_size > self._MAX_ZIP_MEMBER_BYTES:
                    raise UserError(_(
                        'ZIP member %s exceeds the per-file size limit.'
                    ) % info.filename)
                total_size += info.file_size
                if total_size > self._MAX_ZIP_EXTRACTED_BYTES:
                    raise UserError(_(
                        'Backup ZIP exceeds the total extracted size limit.'
                    ))

            for required in ('dump.sql', 'manifest.json'):
                info = by_name.get(required)
                if info is None or info.is_dir():
                    raise UserError(_('ZIP backup missing %s file.') % required)

            manifest_info = by_name['manifest.json']
            if manifest_info.file_size > self._MAX_MANIFEST_BYTES:
                raise UserError(_('ZIP manifest.json exceeds the size limit.'))
            with zf.open(manifest_info) as manifest_file:
                manifest_data = manifest_file.read(self._MAX_MANIFEST_BYTES + 1)
            if len(manifest_data) > self._MAX_MANIFEST_BYTES:
                raise UserError(_('ZIP manifest.json exceeds the size limit.'))
            try:
                manifest = json.loads(manifest_data.decode('utf-8'))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise UserError(_('ZIP manifest.json is not valid JSON: %s') % exc) from exc
            if not isinstance(manifest, dict):
                raise UserError(_('ZIP manifest.json must contain a JSON object.'))
            self._validate_database_name(manifest.get('db_name'))

            # testzip() decompresses every member and checks its CRC.  It must
            # happen before extraction and before any database operation.
            bad_member = zf.testzip()
            if bad_member:
                raise UserError(_('Corrupt entry in ZIP backup: %s') % bad_member)
            return manifest
        except UserError:
            raise
        except Exception as exc:
            raise UserError(_('Backup file is not a valid ZIP archive: %s') % exc) from exc

    @classmethod
    def _extract_zip_info(cls, zf, info, destination_root, relative_name):
        """Stream one validated member to a path beneath destination_root."""
        cls._validate_zip_info(info)
        parts = relative_name.split('/') if relative_name else []
        if not parts or any(part in ('', '.', '..') for part in parts):
            raise UserError(_('Refusing unsafe ZIP member path: %s') % info.filename)
        base = os.path.realpath(destination_root)
        destination = os.path.realpath(os.path.join(destination_root, *parts))
        if destination != base and not destination.startswith(base + os.sep):
            raise UserError(_(
                'Refusing to extract "%s": the path escapes the destination.'
            ) % info.filename)

        if info.is_dir():
            os.makedirs(destination, mode=0o700, exist_ok=True)
            return destination

        os.makedirs(os.path.dirname(destination), mode=0o700, exist_ok=True)
        with zf.open(info) as source, open(destination, 'xb') as target:
            shutil.copyfileobj(source, target, length=1024 * 1024)
        if os.path.getsize(destination) != info.file_size:
            raise UserError(_('Extracted ZIP member has an unexpected size: %s') % info.filename)
        return destination

    def _safe_extract(self, zf, name, tmp_dir):
        """Extract one ZIP member without allowing paths outside tmp_dir."""
        parts = self._zip_member_parts(name)
        return self._extract_zip_info(zf, zf.getinfo(name), tmp_dir, '/'.join(parts))

    @staticmethod
    def _verify_staged_filestore(staged_filestore, expected_files):
        """Verify exact paths, regular-file types and sizes after extraction."""
        actual_files = {}
        for dirpath, dirnames, filenames in os.walk(staged_filestore, followlinks=False):
            for name in dirnames + filenames:
                path = os.path.join(dirpath, name)
                if os.path.islink(path):
                    raise UserError(_('Symlink found in staged filestore: %s') % path)
            for name in filenames:
                path = os.path.join(dirpath, name)
                if not os.path.isfile(path):
                    raise UserError(_('Non-regular file in staged filestore: %s') % path)
                relative = os.path.relpath(path, staged_filestore)
                actual_files[relative] = os.path.getsize(path)
        if actual_files != expected_files:
            raise UserError(_('Staged filestore does not match the backup ZIP.'))

    # ── PostgreSQL helpers ──────────────────────────────────────────────

    def _run_pg_tool(self, tool, args, timeout=300):
        """Run a configured PostgreSQL tool with Odoo's connection environment."""
        command = [find_pg_tool(tool), *args]
        try:
            result = subprocess.run(
                command,
                env=exec_pg_environ(),
                capture_output=True,
                encoding='utf-8',
                errors='replace',
                timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise UserError(_('PostgreSQL %s failed: %s') % (tool, exc)) from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or 'unknown error').strip()
            raise UserError(_(
                'PostgreSQL %s failed (exit %s): %s'
            ) % (tool, result.returncode, detail[:1000]))
        return result

    def _database_exists(self, database):
        """Check existence using a validated, quoted database-name literal."""
        self._validate_database_name(database)
        result = self._run_pg_tool('psql', [
            '--dbname=postgres', '-X', '-q', '-tA',
            '-c', 'SELECT 1 FROM pg_database WHERE datname = %s;' % (
                self._quote_pg_literal(database)
            ),
        ])
        return result.stdout.strip() == '1'

    @staticmethod
    def _close_database_connections(database):
        """Drop Odoo's registry and pooled connections before a rename/drop."""
        try:
            odoo.modules.registry.Registry.delete(database)
        finally:
            odoo.sql_db.close_db(database)

    def _terminate_database(self, database):
        self._validate_database_name(database)
        self._close_database_connections(database)
        self._run_pg_tool('psql', [
            '--dbname=postgres', '-X', '-q', '-v', 'ON_ERROR_STOP=1',
            '-c', 'SELECT pg_terminate_backend(pid) FROM pg_stat_activity '
                  'WHERE datname = %s AND pid <> pg_backend_pid();' % (
                      self._quote_pg_literal(database)
                  ),
        ])

    def _create_database(self, database):
        self._run_pg_tool('createdb', [
            '-T', 'template0', '--encoding=UTF8',
            '--lc-collate=C', '--lc-ctype=C', database,
        ])

    def _drop_database(self, database):
        """Drop a scratch/rollback database; never used on the live target."""
        try:
            self._terminate_database(database)
        except Exception as exc:
            _logger.warning('Could not terminate connections to %s: %s', database, exc)
        self._run_pg_tool('dropdb', ['--if-exists', database])

    def _rename_databases(self, renames):
        """Apply one or more database renames in a single transaction."""
        if not renames:
            return
        for source, _target in renames:
            self._validate_database_name(source)
            self._validate_database_name(_target)
            # Terminate every source before opening the rename transaction.
            self._terminate_database(source)
        statements = [
            'ALTER DATABASE %s RENAME TO %s;' % (
                self._quote_pg_identifier(source),
                self._quote_pg_identifier(target),
            )
            for source, target in renames
        ]
        self._run_pg_tool('psql', [
            '--dbname=postgres', '-X', '-q', '-v', 'ON_ERROR_STOP=1',
            '--single-transaction', '-c', ' '.join(statements),
        ])

    def _database_cutover(self, staging_database, target_database, target_exists):
        """Atomically exchange staging and target using catalog renames."""
        rollback_database = self._scratch_database_name('rollback')
        state = {
            'rollback_database': rollback_database,
            'old_moved': False,
            'new_moved': False,
        }
        renames = []
        if target_exists:
            renames.append((target_database, rollback_database))
        renames.append((staging_database, target_database))
        try:
            self._log('Atomically cutting over to target database: %s' % target_database)
            self._rename_databases(renames)
            state['old_moved'] = target_exists
            state['new_moved'] = True
            return state
        except Exception as exc:
            # PostgreSQL rolls back the whole rename transaction if any
            # statement fails, so there are no partial names to repair here.
            raise UserError(_('Database cutover failed: %s') % exc) from exc

    def _rollback_database_cutover(self, state, staging_database, target_database):
        """Atomically restore both database names after a failed cutover."""
        renames = []
        if state['new_moved']:
            renames.append((target_database, staging_database))
        if state['old_moved']:
            renames.append((state['rollback_database'], target_database))
        try:
            self._rename_databases(renames)
        except Exception as exc:
            raise UserError(_('Database rollback failed: %s') % exc) from exc
        state['new_moved'] = False
        state['old_moved'] = False

    # ── Filestore staging and exchange ─────────────────────────────────

    @staticmethod
    def _filestore_rollback_path(target_filestore):
        directory, name = os.path.split(target_filestore)
        return os.path.join(directory, '.%s.bv_rollback_%s' % (name, uuid.uuid4().hex))

    def _swap_filestore(self, staged_filestore, target_filestore):
        """Exchange filestores on one filesystem without deleting either copy."""
        rollback_filestore = self._filestore_rollback_path(target_filestore)
        state = {
            'rollback_filestore': rollback_filestore,
            'old_moved': False,
            'new_moved': False,
        }
        try:
            if os.path.lexists(target_filestore):
                if os.path.lexists(rollback_filestore):
                    raise UserError(_('Filestore rollback path already exists.'))
                os.rename(target_filestore, rollback_filestore)
                state['old_moved'] = True
            if staged_filestore is not None:
                if not os.path.isdir(staged_filestore) or os.path.islink(staged_filestore):
                    raise UserError(_('Staged filestore is missing or invalid.'))
                os.rename(staged_filestore, target_filestore)
                state['new_moved'] = True
            return state
        except Exception as exc:
            try:
                self._rollback_filestore(state, staged_filestore, target_filestore)
            except Exception as rollback_exc:
                raise UserError(_(
                    'Filestore cutover failed and rollback also failed: %s; %s'
                ) % (exc, rollback_exc)) from exc
            raise

    def _rollback_filestore(self, state, staged_filestore, target_filestore):
        """Restore both filestore paths after a failed exchange."""
        errors = []
        if state['new_moved']:
            try:
                os.rename(target_filestore, staged_filestore)
                state['new_moved'] = False
            except Exception as exc:
                errors.append(str(exc))
        if state['old_moved']:
            try:
                os.rename(state['rollback_filestore'], target_filestore)
                state['old_moved'] = False
            except Exception as exc:
                errors.append(str(exc))
        if errors:
            raise UserError(_('Filestore rollback failed: %s') % '; '.join(errors))

    # ── Restore preparation ─────────────────────────────────────────────

    def _validate_custom_dump(self, dump_path):
        """Require pg_restore to parse the complete custom-format archive."""
        self._run_pg_tool('pg_restore', ['--list', dump_path], timeout=300)

    def _prepare_restore(self, backup_path, tmp_dir):
        """Fully validate a backup and stage its filestore on the target FS."""
        if zipfile.is_zipfile(backup_path):
            with zipfile.ZipFile(backup_path, 'r') as zf:
                self._validate_zip_archive(zf)
                restore_source = self._safe_extract(zf, 'dump.sql', tmp_dir)
                filestore_infos = [
                    info for info in zf.infolist()
                    if info.filename.startswith('filestore/')
                ]
                staged_filestore = None
                if filestore_infos:
                    target_filestore = config.filestore(self.target_database)
                    filestore_root = os.path.dirname(target_filestore)
                    os.makedirs(filestore_root, mode=0o700, exist_ok=True)
                    staged_filestore = tempfile.mkdtemp(
                        prefix='.%s.restore-' % self.target_database,
                        dir=filestore_root,
                    )
                    expected_files = {}
                    try:
                        for info in filestore_infos:
                            if info.filename == 'filestore/':
                                continue
                            relative_name = info.filename[len('filestore/'):]
                            if relative_name and not info.is_dir():
                                expected_files[relative_name] = info.file_size
                            self._extract_zip_info(
                                zf, info, staged_filestore, relative_name.rstrip('/'),
                            )
                        self._verify_staged_filestore(staged_filestore, expected_files)
                    except Exception:
                        shutil.rmtree(staged_filestore, ignore_errors=True)
                        raise
                self._log('ZIP integrity, manifest and filestore validation passed')
                return {
                    'method': 'psql',
                    'source': restore_source,
                    'filestore': staged_filestore,
                }

        self._validate_custom_dump(backup_path)
        self._log('PostgreSQL custom dump validation passed')
        return {
            'method': 'pg_restore',
            'source': backup_path,
            'filestore': None,
        }

    _DUMP_RESTRICT_RE = re.compile(r'^\s*\\(un)?restrict\b')

    def _strip_dump_restrict(self, dump_path):
        """Write a copy of *dump_path* without pg_dump >= 18 guard lines."""
        stripped_path = dump_path + '.norestrict'
        with open(dump_path, 'r', encoding='utf-8', errors='surrogateescape') as source, \
                open(stripped_path, 'w', encoding='utf-8', errors='surrogateescape') as target:
            for line in source:
                if self._DUMP_RESTRICT_RE.match(line):
                    continue
                target.write(line)
        return stripped_path

    def _restore_staging_database(self, staging_database, restore):
        """Restore into staging with psql meta-commands locked down."""
        if restore['method'] == 'psql':
            restrict_token = secrets.token_hex()
            # pg_dump >= 18 emits its own guard lines (\restrict KEY ... \unrestrict
            # KEY).  psql rejects a nested \restrict ("only \unrestrict is
            # allowed"), so a dump carrying its own guard fails under ours.
            # Strip the dump's lines and keep our outer guard.
            restore_source = self._strip_dump_restrict(restore['source'])
            args = [
                '--dbname=' + staging_database,
                '-X', '-q', '-v', 'ON_ERROR_STOP=1', '--single-transaction',
                '-c', '\\restrict ' + restrict_token,
                '-f', restore_source,
            ]
            self._run_pg_tool('psql', args, timeout=self._RESTORE_TIMEOUT)
        else:
            self._run_pg_tool('pg_restore', [
                '--dbname=' + staging_database,
                '--no-owner', '--exit-on-error', restore['source'],
            ], timeout=self._RESTORE_TIMEOUT)

    def _verify_staging_database(self, staging_database):
        """Load the Odoo registry and query it before allowing a cutover."""
        try:
            registry = odoo.modules.registry.Registry.new(staging_database)
            with registry.cursor() as cr:
                env = odoo.api.Environment(cr, odoo.api.SUPERUSER_ID, {})
                env['ir.module.module'].search([], limit=1)
        except Exception as exc:
            raise UserError(_(
                'Staging database verification failed: %s'
            ) % exc) from exc
        finally:
            self._close_database_connections(staging_database)

    def _neutralize_staging_database(self, staging_database):
        """Use Odoo's helper and abort the restore if neutralization fails."""
        try:
            with odoo.sql_db.db_connect(staging_database).cursor() as cr:
                odoo.modules.neutralize.neutralize_database(cr)
        except Exception as exc:
            raise UserError('Database neutralization failed: %s' % exc) from exc
        finally:
            self._close_database_connections(staging_database)

    # ── Actions ─────────────────────────────────────────────────────────

    def action_list_backups(self):
        """Fetch and display available backups from the selected provider."""
        self.ensure_one()
        provider = self._get_provider()

        try:
            backups = provider.list_backups()
            if not backups:
                self.available_backups = 'No backups found on this provider.'
                return {
                    'type': 'ir.actions.client',
                    'tag': 'display_notification',
                    'params': {
                        'title': 'No Backups',
                        'message': 'No backup files found on the selected provider.',
                        'type': 'warning',
                        'sticky': False,
                    },
                }

            lines = []
            for b in backups[:50]:  # show max 50
                size = b.get('size_gb', 0)
                mtime = b.get('modified_date', '')
                if isinstance(mtime, datetime):
                    mtime = mtime.strftime('%Y-%m-%d %H:%M')
                lines.append(
                    '%-80s %8.2f GB  %s' % (
                        b['filename'], size, mtime or ''
                    )
                )
            self.available_backups = '\n'.join(lines)

            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Backups Found',
                    'message': 'Found %d backup(s). Copy the filename into the "Backup Filename" field.' % len(backups),
                    'type': 'success',
                    'sticky': False,
                },
            }
        except Exception as e:
            self.available_backups = 'Error: %s' % e
            raise UserError(_('Failed to list backups: %s') % e)

    def action_restore(self):
        """Validate, restore to staging, then cut over with rollback."""
        self.ensure_one()
        self._assert_system_admin()

        if not self.selected_backup:
            raise UserError(_('Please select a backup file to restore.'))
        if not self.target_database:
            raise UserError(_('Please enter a target database name.'))

        self._validate_database_name(self.target_database)
        self._validate_restore_options()
        selected_backup = self._sanitize_backup_filename(self.selected_backup)

        self.state = 'restoring'
        self.restore_log = ''
        provider = self._get_provider()

        tmp_dir = None
        staging_database = None
        staged_filestore = None
        restore = None
        database_state = None
        filestore_state = None

        try:
            tmp_dir = tempfile.mkdtemp(prefix='backup_restore_')
            self.env['backup.job']._mark_temp_dir_owner(tmp_dir)
            self._log('Created temp dir: %s' % tmp_dir)

            dl_path = os.path.join(tmp_dir, selected_backup)
            self._log('Downloading %s from %s...' % (selected_backup, provider.name))
            result = provider.download_backup(selected_backup, dl_path)
            if not result.get('success'):
                raise UserError(_('Download failed: %s') % (
                    result.get('message', 'Unknown error')
                ))

            if not os.path.isfile(dl_path):
                # rclone's "copy" may not preserve the filename.  Ignore the
                # owner marker and require one unambiguous downloaded file.
                candidates = [
                    os.path.join(tmp_dir, name)
                    for name in os.listdir(tmp_dir)
                    if name != self.env['backup.job']._TEMP_OWNER_MARKER
                    and os.path.isfile(os.path.join(tmp_dir, name))
                ]
                if len(candidates) != 1:
                    raise UserError(_(
                        'Download did not produce exactly one readable backup file.'
                    ))
                dl_path = candidates[0]
            if not os.path.isfile(dl_path) or os.path.getsize(dl_path) == 0:
                raise UserError(_('Downloaded backup file is missing or empty.'))
            self._log('Downloaded: %s' % dl_path)

            if self.backup_is_encrypted:
                if not self.encryption_key:
                    raise UserError(_(
                        'Backup is marked as encrypted but no encryption key provided.'
                    ))
                self._log('Decrypting...')
                from odoo.addons.backup_versatile.models.backup_job import BackupJob
                dec_path = os.path.join(
                    tmp_dir, 'decrypted_%s' % selected_backup.replace('.enc', ''),
                )
                BackupJob.decrypt_backup_file(dl_path, dec_path, self.encryption_key)
                dl_path = dec_path
                if not os.path.isfile(dl_path) or os.path.getsize(dl_path) == 0:
                    raise UserError(_('Decrypted backup file is missing or empty.'))
                self._log('Decrypted successfully')

            # This includes CRC/testzip, manifest validation, safe extraction
            # and pg_restore --list.  No target operation may precede it.
            restore = self._prepare_restore(dl_path, tmp_dir)
            staged_filestore = restore['filestore']
            self._log('Backup format validated: %s' % restore['method'].upper())

            target_exists = self._database_exists(self.target_database)
            if target_exists and not self.drop_existing:
                raise UserError(_(
                    'Database "%s" already exists. Check "Drop Existing Database" '
                    'to replace it after a successful staged restore.'
                ) % self.target_database)

            staging_database = self._scratch_database_name('restore')
            self._log('Creating fresh staging database: %s' % staging_database)
            self._create_database(staging_database)
            self._restore_staging_database(staging_database, restore)
            self._log('Staging restore succeeded; verifying database...')
            self._verify_staging_database(staging_database)

            if self.neutralize_database:
                self._log('Neutralizing staging database with Odoo helper...')
                self._neutralize_staging_database(staging_database)
                self._log('Staging database neutralized')

            target_filestore = config.filestore(self.target_database)
            os.makedirs(os.path.dirname(target_filestore), mode=0o700, exist_ok=True)

            self._log('Cutting over to target database: %s' % self.target_database)
            database_state = self._database_cutover(
                staging_database, self.target_database, target_exists,
            )

            try:
                filestore_state = self._swap_filestore(
                    staged_filestore, target_filestore,
                )
            except Exception as filestore_exc:
                # _swap_filestore first restores the filesystem itself.  The
                # database names must then be restored as well.
                self._rollback_database_cutover(
                    database_state, staging_database, self.target_database,
                )
                raise UserError(_(
                    'Filestore cutover failed; database restore was rolled back: %s'
                ) % filestore_exc) from filestore_exc

            staged_filestore = None
            staging_database = None
            self._log('Filestore cutover completed')

            # The old target is no longer needed.  Cleanup failures are logged
            # but cannot turn a successful cutover into a reported failure.
            if database_state['old_moved']:
                try:
                    self._drop_database(database_state['rollback_database'])
                except Exception as exc:
                    _logger.warning(
                        'Could not remove rollback database %s: %s',
                        database_state['rollback_database'], exc,
                    )
                    self._log('WARNING: rollback database was retained: %s' % (
                        database_state['rollback_database']
                    ))
            if filestore_state['old_moved']:
                try:
                    shutil.rmtree(filestore_state['rollback_filestore'])
                except Exception as exc:
                    _logger.warning(
                        'Could not remove rollback filestore %s: %s',
                        filestore_state['rollback_filestore'], exc,
                    )
                    self._log('WARNING: rollback filestore was retained: %s' % (
                        filestore_state['rollback_filestore']
                    ))

            self.state = 'done'
            self._log('Restore completed successfully!')
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Restore Successful',
                    'message': 'Database "%s" restored successfully.' % self.target_database,
                    'type': 'success',
                    'sticky': True,
                },
            }

        except Exception as exc:
            self.state = 'select'
            self._log('ERROR: %s' % exc)
            if isinstance(exc, UserError):
                raise
            raise UserError(_('Restore failed: %s') % exc) from exc

        finally:
            # A failed cutover may have moved the new DB back to its staging
            # name.  A successful one set staging_database to None above.
            if staging_database:
                try:
                    self._drop_database(staging_database)
                except Exception as exc:
                    _logger.warning('Could not remove staging database %s: %s', staging_database, exc)
            if staged_filestore and os.path.lexists(staged_filestore):
                try:
                    shutil.rmtree(staged_filestore, ignore_errors=True)
                except Exception as exc:
                    _logger.warning('Could not remove staged filestore %s: %s', staged_filestore, exc)
            if tmp_dir and os.path.exists(tmp_dir):
                shutil.rmtree(tmp_dir, ignore_errors=True)

    # ── Helpers ─────────────────────────────────────────────────────────

    def _log(self, message):
        """Add message to restore log."""
        ts = datetime.now().strftime('%H:%M:%S')
        entry = '[%s] %s' % (ts, message)
        if self.restore_log:
            self.restore_log += '\n' + entry
        else:
            self.restore_log = entry
        _logger.info('Restore: %s', message)
