# -*- coding: utf-8 -*-

import base64
import logging
import os
import re
import subprocess
import tempfile
from datetime import datetime

from odoo import models, fields, api, _
from odoo.exceptions import UserError
from odoo.tools import config

_logger = logging.getLogger(__name__)


class BackupRestoreWizard(models.TransientModel):
    """Wizard to restore a backup from any configured provider.

    Flow:
    1. User selects a provider (local / SFTP / cloud)
    2. Wizard lists available backups from that provider
    3. User selects a backup file
    4. Optionally: enter a new database name (defaults to original)
    5. Wizard downloads, decrypts (if encrypted), and restores the database
    """
    _name = 'backup.restore.wizard'
    _description = 'Restore Backup Wizard'

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
        help='If a database with the same name exists, drop it before restore. '
             'WARNING: this permanently deletes the existing database.',
    )

    # ── Encryption (if the backup was encrypted) ────────────────────────

    backup_is_encrypted = fields.Boolean(
        string='Encrypted Backup',
        default=False,
        help='Check this if the backup was encrypted with AES-256-GCM',
    )
    encryption_key = fields.Char(
        string='Encryption Key (base64)',
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
        raise UserError('Please select a provider first.')

    def _validate_restore_options(self):
        """Reject option combinations that would destroy data."""
        self.ensure_one()
        if self.drop_existing and not self.create_database:
            raise UserError(
                'Combining "Drop Existing Database" with "Create Database" '
                'disabled would drop the database and never restore it. '
                'Enable "Create Database" (or disable "Drop Existing Database").'
            )
        # Never target the database this instance is serving: the restore
        # would drop/overwrite the registry executing this very code, and the
        # filestore swap below would remove its attachments as well.
        if self.target_database == self.env.cr.dbname:
            raise UserError(
                'The target database "%s" is the database this Odoo instance is '
                'currently running on. Restoring on top of it would destroy the '
                'live database. Choose a different target database name.'
                % self.target_database
            )

    @staticmethod
    def _sanitize_backup_filename(filename):
        """Return a safe basename for the backup file, rejecting traversal."""
        if not filename:
            return filename
        if '\x00' in filename:
            raise UserError('Invalid backup filename: null byte not allowed.')
        safe = os.path.basename(filename.rstrip('/'))
        if safe in ('', '.', '..'):
            raise UserError(
                'Invalid backup filename. Provide a plain file name (no folders).'
            )
        return safe

    @staticmethod
    def _safe_extract(zf, name, tmp_dir):
        """Extract one zip member, refusing paths that escape ``tmp_dir``.

        The archive comes from an external store (SFTP/bucket). A crafted entry
        such as ``filestore/../../etc/cron.d/x`` (zip-slip) must never be
        written outside the temp directory we control.
        """
        base = os.path.realpath(tmp_dir)
        dest = os.path.realpath(os.path.join(tmp_dir, name))
        if dest != base and not dest.startswith(base + os.sep):
            raise UserError(
                'Refusing to extract "%s": the path escapes the temp directory.' % name
            )
        return zf.extract(name, tmp_dir)

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
                    '%-50s %8.2f GB  %s' % (
                        b['filename'][:50], size, mtime or ''
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
            raise UserError('Failed to list backups: %s' % e)

    def action_restore(self):
        """Execute the restore process."""
        self.ensure_one()

        if not self.selected_backup:
            raise UserError('Please select a backup file to restore.')
        if not self.target_database:
            raise UserError('Please enter a target database name.')

        # Validate database name
        if not re.match(r'^[a-zA-Z0-9_][a-zA-Z0-9_.-]*$', self.target_database):
            raise UserError(
                'Invalid database name. Use only letters, numbers, underscores, hyphens, and dots.'
            )

        # Data-loss guard: dropping without recreating would destroy the DB.
        self._validate_restore_options()

        # Never trust a free-text filename: keep only a plain basename.
        selected_backup = self._sanitize_backup_filename(self.selected_backup)

        self.state = 'restoring'
        self.restore_log = ''

        provider = self._get_provider()

        try:
            # ── Step 1: Create temp dir for download ──────────────────
            tmp_dir = tempfile.mkdtemp(prefix='backup_restore_')
            # Mark the owner so the stale-temp sweep can reclaim it if this
            # worker dies before the finally block runs.
            self.env['backup.job']._mark_temp_dir_owner(tmp_dir)
            self._log('Created temp dir: %s' % tmp_dir)

            # ── Step 2: Download backup ───────────────────────────────
            dl_path = os.path.join(tmp_dir, selected_backup)
            self._log('Downloading %s from %s...' % (selected_backup, provider.name))
            result = provider.download_backup(selected_backup, dl_path)

            if not result.get('success'):
                raise UserError('Download failed: %s' % result.get('message', 'Unknown error'))

            if not os.path.exists(dl_path):
                # rclone's "copy" may not preserve the filename — find the file
                for f in os.listdir(tmp_dir):
                    dl_path = os.path.join(tmp_dir, f)
                    break

            self._log('Downloaded: %s' % dl_path)

            # ── Step 3: Decrypt if needed ─────────────────────────────
            if self.backup_is_encrypted:
                if not self.encryption_key:
                    raise UserError(
                        'Backup is marked as encrypted but no encryption key provided.'
                    )
                self._log('Decrypting...')
                from odoo.addons.backup_versatile.models.backup_job import BackupJob
                dec_path = os.path.join(tmp_dir, 'decrypted_%s' % selected_backup.replace('.enc', ''))
                BackupJob.decrypt_backup_file(dl_path, dec_path, self.encryption_key)
                dl_path = dec_path
                self._log('Decrypted successfully')

            # ── Step 4: Determine backup format ───────────────────────
            is_zip = dl_path.endswith('.zip')
            self._log('Backup format: %s' % ('ZIP' if is_zip else 'PG Dump'))

            # ── Step 5: Prepare target database ───────────────────────
            self._log('Preparing target database: %s' % self.target_database)

            # Get PostgreSQL connection parameters
            pg_host = config.get('db_host', 'localhost')
            pg_port = config.get('db_port', 5432)
            pg_user = config.get('db_user', 'odoo')
            pg_password = config.get('db_password', '')

            env = os.environ.copy()
            if pg_password:
                env['PGPASSWORD'] = pg_password

            # Check if database exists
            check_cmd = [
                'psql', '-h', pg_host, '-p', str(pg_port),
                '-U', pg_user, '-d', 'postgres',
                '-t', '-c',
                "SELECT 1 FROM pg_database WHERE datname='%s'" % self.target_database,
            ]
            exists = subprocess.run(
                check_cmd, env=env, capture_output=True, text=True,
            ).stdout.strip() == '1'

            if exists:
                if self.drop_existing:
                    # Terminate connections and drop
                    self._log('Dropping existing database: %s' % self.target_database)
                    subprocess.run(
                        ['psql', '-h', pg_host, '-p', str(pg_port),
                         '-U', pg_user, '-d', 'postgres',
                         '-c', "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                               "WHERE datname='%s' AND pid <> pg_backend_pid()" % self.target_database],
                        env=env, check=True, capture_output=True,
                    )
                    subprocess.run(
                        ['dropdb', '-h', pg_host, '-p', str(pg_port),
                         '-U', pg_user, '--if-exists', self.target_database],
                        env=env, check=True, capture_output=True,
                    )
                else:
                    raise UserError(
                        'Database "%s" already exists. '
                        'Check "Drop Existing Database" to overwrite it.' % self.target_database
                    )

            # ── Step 6: Create database ───────────────────────────────
            if self.create_database:
                self._log('Creating database: %s' % self.target_database)
                subprocess.run(
                    ['createdb', '-h', pg_host, '-p', str(pg_port),
                     '-U', pg_user, '-T', 'template0',
                     '--encoding=UTF8', '--lc-collate=C', '--lc-ctype=C',
                     self.target_database],
                    env=env, check=True, capture_output=True,
                )

            # ── Step 7: Restore ────────────────────────────────────────
            self._log('Restoring backup...')

            # Filestore is extracted to the temp dir now but swapped in only
            # AFTER the database restore succeeds (see Step 7b), so a failed
            # restore can never leave the target DB without its attachments.
            pending_filestore = None
            if is_zip:
                # Extract dump.sql from zip and restore via psql (plain SQL)
                import zipfile
                with zipfile.ZipFile(dl_path, 'r') as zf:
                    # Extract dump.sql (zip-slip safe)
                    restore_source = self._safe_extract(zf, 'dump.sql', tmp_dir)
                    restore_method = 'psql'

                    # Extract filestore if present
                    filestore_path = config.get('data_dir')
                    if not filestore_path:
                        raise UserError(
                            'Cannot determine Odoo data_dir. '
                            'Make sure Odoo is configured properly.'
                        )
                    target_filestore = os.path.join(
                        filestore_path, 'filestore', self.target_database
                    )
                    for name in zf.namelist():
                        if name.startswith('filestore/'):
                            self._safe_extract(zf, name, tmp_dir)

                    # Keep the extracted filestore aside; the swap with the
                    # live one happens after a successful restore.
                    extracted_filestore = os.path.join(tmp_dir, 'filestore')
                    if os.path.exists(extracted_filestore):
                        pending_filestore = (extracted_filestore, target_filestore)
            else:
                # PostgreSQL custom-format dump (pg_dump --format=c) must be
                # restored with pg_restore — psql -f cannot read it.
                restore_source = dl_path
                restore_method = 'pg_restore'

            if restore_method == 'psql':
                # Plain SQL dump (extracted from zip). --single-transaction
                # keeps a failure from leaving a half-imported database behind
                # (pg_dump plain output has no CREATE DATABASE / VACUUM, so it
                # is safe to run inside one transaction).
                restore_cmd = [
                    'psql', '-h', pg_host, '-p', str(pg_port),
                    '-U', pg_user, '-d', self.target_database,
                    '-v', 'ON_ERROR_STOP=1', '--single-transaction',
                    '-f', restore_source,
                ]
            else:
                # PostgreSQL custom-format archive (pg_dump --format=c).
                # --no-owner + --role: objects must end up owned by the DB
                # user, not by whatever role happens to run the restore.
                restore_cmd = [
                    'pg_restore', '-h', pg_host, '-p', str(pg_port),
                    '-U', pg_user, '-d', self.target_database,
                    '--no-owner', '--role', pg_user, '--exit-on-error',
                    restore_source,
                ]

            restore_result = subprocess.run(
                restore_cmd, env=env, capture_output=True, text=True,
                timeout=3600,
            )

            if restore_result.returncode != 0:
                stderr = restore_result.stderr.strip()[:500]
                raise UserError('Restore failed: %s' % stderr)

            self._log('Database restored successfully')

            # ── Step 7b: Swap in the extracted filestore (Step 7 succeeded,
            # so replacing the old one can no longer destroy data) ────────
            if pending_filestore:
                import shutil
                extracted_filestore, target_filestore = pending_filestore
                os.makedirs(os.path.dirname(target_filestore), exist_ok=True)
                if os.path.exists(target_filestore):
                    shutil.rmtree(target_filestore)
                os.rename(extracted_filestore, target_filestore)
                self._log('Filestore restored to: %s' % target_filestore)

            # ── Step 8: Neutralize if requested ───────────────────────
            if self.neutralize_database:
                self._log('Neutralizing database (disabling crons, mail, payments)...')
                # Disable crons
                subprocess.run(
                    ['psql', '-h', pg_host, '-p', str(pg_port),
                     '-U', pg_user, '-d', self.target_database,
                     '-c', "UPDATE ir_cron SET active = false"],
                    env=env, capture_output=True,
                )
                # Disable outgoing mail servers
                subprocess.run(
                    ['psql', '-h', pg_host, '-p', str(pg_port),
                     '-U', pg_user, '-d', self.target_database,
                     '-c', "UPDATE ir_mail_server SET active = false"],
                    env=env, capture_output=True,
                )
                # Disable payment providers
                subprocess.run(
                    ['psql', '-h', pg_host, '-p', str(pg_port),
                     '-U', pg_user, '-d', self.target_database,
                     '-c', "UPDATE payment_provider SET state = 'disabled'"],
                    env=env, capture_output=True,
                )
                self._log('Database neutralized')

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

        except Exception as e:
            self.state = 'select'
            self._log('ERROR: %s' % e)
            raise UserError('Restore failed: %s' % e)

        finally:
            # Cleanup temp dir
            if 'tmp_dir' in dir() and tmp_dir and os.path.exists(tmp_dir):
                import shutil
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
