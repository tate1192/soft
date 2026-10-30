"""Pre-flight checks: a backup must fail *before* the dump, not after it.

The module ships '/home/backups' as the default directory of both provider
types. A provider created through a quick-create dialog only gets a name, so
the field keeps that default and every backup then died with a bare
"[Errno 13] Permission denied: '/home/backups'" after a full database dump.
These tests pin the behaviour that replaced it.
"""

import contextlib
import os
import shutil
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

# Root bypasses every filesystem permission check, so the unwritable
# scenarios cannot be reproduced there and are skipped instead of lying.
IS_ROOT = getattr(os, 'geteuid', lambda: 1)() == 0


@contextlib.contextmanager
def _fake_session(sftp):
    yield types.SimpleNamespace(
        client=MagicMock(), sftp=sftp, pinned_fingerprint=None)


@tagged('backup_versatile', 'standard')
class TestProviderPreflight(TransactionCase):
    """Pre-flight validation of provider destinations."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Provider = cls.env['backup.provider.local']
        cls.SftpProvider = cls.env['backup.provider.sftp']
        cls.Config = cls.env['backup.config']
        cls.Job = cls.env['backup.job']

    def setUp(self):
        super().setUp()
        self.temp_dirs = []

    def tearDown(self):
        for path in self.temp_dirs:
            if os.path.isdir(path):
                os.chmod(path, 0o700)
                shutil.rmtree(path, ignore_errors=True)
        super().tearDown()

    # ---------------------------------------------------------------- helpers
    def _temp_dir(self, mode=None):
        path = tempfile.mkdtemp(prefix='bv_preflight_')
        self.temp_dirs.append(path)
        if mode is not None:
            os.chmod(path, mode)
        return path

    def _local_provider(self, directory=None, **vals):
        values = {
            'name': 'Preflight local provider',
            'backup_directory': directory or self._temp_dir(),
            'create_subdirectories': False,
            'check_disk_space': True,
            'min_free_space_gb': 0.0001,
        }
        values.update(vals)
        return self.Provider.create(values)

    def _sftp_provider(self, **vals):
        values = {
            'name': 'Preflight sftp provider',
            'hostname': 'sftp.example.com',
            'port': 22,
            'username': 'backupuser',
            'auth_type': 'password',
            'password': 'secret',
            'remote_directory': '/data/backups',
            'create_remote_dirs': True,
        }
        values.update(vals)
        return self.SftpProvider.create(values)

    def _config(self, provider):
        return self.Config.create({
            'name': 'Preflight config',
            'database_name': self.env.cr.dbname,
            'backup_format': 'zip',
            'local_provider_ids': [(4, provider.id)],
            'retention_policy': 'count',
            'retention_count': 1,
            'notify_success': False,
            'notify_failure': False,
        })

    # ------------------------------------------------------ local: preflight
    def test_preflight_passes_for_writable_directory(self):
        directory = self._temp_dir()
        provider = self._local_provider(directory)

        result = provider._preflight_check()

        self.assertTrue(result['success'], result['message'])
        self.assertIn(directory, result['message'])
        # The writability probe must clean up after itself
        self.assertNotIn('.backup_versatile_write_test', os.listdir(directory))

    @unittest.skipIf(IS_ROOT, 'root bypasses filesystem permission checks')
    def test_preflight_reports_unwritable_directory(self):
        directory = self._temp_dir(mode=0o500)
        provider = self._local_provider(directory)

        result = provider._preflight_check()

        self.assertFalse(result['success'])
        self.assertIn(directory, result['message'])
        self.assertIn('Backup Directory', result['message'])
        # The message must point at the field to fix, not just at the OS error
        self.assertIn('Directory Configuration', result['message'])

    @unittest.skipIf(IS_ROOT, 'root bypasses filesystem permission checks')
    @unittest.skipIf(os.path.isdir('/home/backups'), 'default directory exists here')
    def test_preflight_reports_default_directory_as_unusable(self):
        """The shipped default '/home/backups' is the trap that started this."""
        provider = self._local_provider('/home/backups')

        result = provider._preflight_check()

        self.assertFalse(result['success'])
        self.assertIn('/home/backups', result['message'])
        self.assertIn('Backup Directory', result['message'])

    def test_preflight_reports_too_little_free_space(self):
        provider = self._local_provider(
            self._temp_dir(), min_free_space_gb=10 ** 6)  # 1 PB required

        result = provider._preflight_check()

        self.assertFalse(result['success'])
        self.assertIn('free space', result['message'])

    # ------------------------------------------------------- local: upload
    @unittest.skipIf(IS_ROOT, 'root bypasses filesystem permission checks')
    def test_upload_error_names_the_directory_field(self):
        directory = self._temp_dir(mode=0o500)
        provider = self._local_provider(directory)
        with tempfile.NamedTemporaryFile(suffix='.zip') as local:
            local.write(b'x' * 16)
            local.flush()

            result = provider.upload_backup(local.name, 'db.zip')

        self.assertFalse(result['success'])
        self.assertIn('Backup Directory', result['message'])
        self.assertIn('Directory Configuration', result['message'])

    # ------------------------------------------------------ job: preflight
    @unittest.skipIf(IS_ROOT, 'root bypasses filesystem permission checks')
    def test_job_is_aborted_before_the_dump(self):
        provider = self._local_provider(self._temp_dir(mode=0o500))
        config = self._config(provider)

        job = config.with_context(manual_execution=False).create_backup()

        self.assertEqual(job.status, 'error')
        self.assertIn('is not ready', job.error_message or '')
        self.assertIn('Backup Directory', job.error_message or '')
        log = job.log_entries or ''
        # The whole point: no dump was created
        self.assertNotIn('DUMP DB', log)
        self.assertNotIn('Backup file created', log)

    def test_job_logs_resolved_destination_and_uploads(self):
        directory = self._temp_dir()
        provider = self._local_provider(directory)
        config = self._config(provider)

        job = config.with_context(manual_execution=False).create_backup()

        self.assertEqual(job.status, 'success', job.error_message)
        log = job.log_entries or ''
        self.assertIn('Pre-flight check passed', log)
        self.assertIn(
            'Uploading to provider: %s -> %s' % (provider.name, directory), log)

        files = [name for name in os.listdir(directory) if name.endswith('.zip')]
        self.assertEqual(len(files), 1)
        # Backups are full dumps: the file must stay owner-only
        self.assertEqual(
            os.stat(os.path.join(directory, files[0])).st_mode & 0o777, 0o600)

    def test_preflight_message_is_not_rewritten_by_error_formatting(self):
        """An actionable message must survive _format_backup_error()."""
        message = (
            "Backup Directory '/home/backups' cannot be used: [Errno 13] "
            "Permission denied: '/home/backups'. Set a directory the Odoo user "
            "can write to in the provider form (Storage Settings > Directory "
            "Configuration).")

        self.assertEqual(
            self.Job._format_backup_error(UserError(message)), message)

    # ------------------------------------------------------- sftp: preflight
    def test_sftp_preflight_requires_remote_directory(self):
        """An empty Remote Directory is caught before the dump.

        The field is required, so an empty string is the only way a record
        without a destination can reach the check.
        """
        provider = self._sftp_provider()
        provider.write({'remote_directory': ''})

        result = provider._preflight_check()

        self.assertFalse(result['success'])
        self.assertIn('Remote Directory', result['message'])

    def test_sftp_preflight_reports_directory_problem(self):
        provider = self._sftp_provider()
        sftp = MagicMock()
        with patch.object(type(provider), '_sftp_session',
                          lambda _self: _fake_session(sftp)), \
                patch.object(
                    type(provider), '_ensure_remote_directory',
                    side_effect=UserError(
                        'Remote directory does not exist: /data/backups')):
            result = provider._preflight_check()

        self.assertFalse(result['success'])
        self.assertIn('Remote directory does not exist', result['message'])

    def test_sftp_preflight_passes_when_directory_is_usable(self):
        provider = self._sftp_provider()
        with patch.object(type(provider), '_sftp_session',
                          lambda _self: _fake_session(MagicMock())):
            result = provider._preflight_check()

        self.assertTrue(result['success'], result['message'])
        self.assertIn(provider.remote_directory, result['message'])

    def test_sftp_permission_denied_is_actionable(self):
        provider = self._sftp_provider()
        error = IOError(13, 'Permission denied', '/data/backups')

        message = provider._describe_connection_error(error)

        self.assertIn('Remote Directory', message)
        self.assertIn(provider.remote_directory, message)

    # ----------------------------------------------------------- reporting
    def test_destination_paths_are_reported(self):
        directory = self._temp_dir()
        local = self._local_provider(directory)
        self.assertEqual(local._get_destination_path(), directory)

        sftp = self._sftp_provider(hostname='sftp.example.com',
                                   remote_directory='/data/backups')
        self.assertEqual(sftp._get_destination_path(),
                         'sftp.example.com:/data/backups')

    def test_abstract_provider_destination_is_empty(self):
        """Cloud providers keep an empty destination (no log noise)."""
        cloud = self.env['backup.provider.cloud'].create({
            'name': 'Preflight cloud provider',
            'bucket': 'test-bucket',
        })
        self.assertEqual(cloud._get_destination_path(), '')

    def test_config_view_disables_quick_create_on_provider_fields(self):
        """Quick create would produce a provider with the default directory."""
        arch = self.env.ref('backup_versatile.view_backup_config_form').arch
        for field_name in ('local_provider_ids', 'sftp_provider_ids',
                           'cloud_provider_ids'):
            start = arch.index('name="%s"' % field_name)
            snippet = arch[start:start + 250]
            self.assertIn('no_quick_create', snippet, field_name)
            self.assertIn('no_create_edit', snippet, field_name)
