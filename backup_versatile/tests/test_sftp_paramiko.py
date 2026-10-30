import base64
import contextlib
import errno
import hashlib
import os
import tempfile
import types
from unittest.mock import MagicMock, patch

import paramiko

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.backup_versatile.models import backup_provider_sftp


@contextlib.contextmanager
def _fake_session(sftp, client=None, pinned_fingerprint=None):
    """Yield a stand-in for the object returned by ``_sftp_session``."""
    yield types.SimpleNamespace(
        client=client if client is not None else MagicMock(),
        sftp=sftp,
        pinned_fingerprint=pinned_fingerprint,
    )


@tagged('backup_versatile', 'standard')
class TestSftpParamikoProvider(TransactionCase):
    """Unit tests for the paramiko-based SFTP provider."""

    def setUp(self):
        super().setUp()
        self.Provider = self.env['backup.provider.sftp']

    def _provider(self, **vals):
        values = {
            'name': 'SFTP test provider',
            'hostname': 'sftp.example.com',
            'port': 22,
            'username': 'backupuser',
            'auth_type': 'password',
            'password': 'secret',
            'remote_directory': '/home/backups',
            'create_remote_dirs': True,
            'verify_host_key': True,
        }
        values.update(vals)
        return self.Provider.create(values)

    def _use_session(self, sftp, client=None, pinned_fingerprint=None):
        """Patch the session factory on the provider model class."""
        return patch.object(
            type(self.Provider),
            '_sftp_session',
            lambda _self: _fake_session(sftp, client, pinned_fingerprint),
        )

    # ---------------------------------------------------------------- deps
    def test_manifest_declares_paramiko_instead_of_asyncssh(self):
        """Regression: asyncssh must not come back as a declared dependency."""
        from odoo.modules.module import get_manifest

        deps = (get_manifest('backup_versatile') or {}).get(
            'external_dependencies', {}).get('python', [])
        self.assertIn('paramiko', deps)
        self.assertNotIn('asyncssh', deps)

    def test_provider_module_does_not_import_asyncssh(self):
        self.assertFalse(hasattr(backup_provider_sftp, 'asyncssh'))
        source = open(backup_provider_sftp.__file__).read()
        self.assertNotIn('import asyncssh', source)
        self.assertIn('import paramiko', source)

    # ------------------------------------------------------- file name safety
    def test_sanitize_remote_filename_strips_directories(self):
        self.assertEqual(
            self.Provider._sanitize_remote_filename('../../etc/passwd'),
            'passwd',
        )

    def test_sanitize_remote_filename_rejects_dotdot(self):
        with self.assertRaises(UserError):
            self.Provider._sanitize_remote_filename('..')

    def test_sanitize_remote_filename_rejects_null_byte(self):
        with self.assertRaises(UserError):
            self.Provider._sanitize_remote_filename('backup\x00.zip')

    def test_is_backup_file(self):
        provider = self._provider()
        for name in ('db.zip', 'db.dump', 'db.sql', 'db.tar.gz', 'DB.TGZ'):
            self.assertTrue(provider._is_backup_file(name), name)
        for name in ('notabackup.txt', 'backup.zip.part', 'notes'):
            self.assertFalse(provider._is_backup_file(name), name)

    # ------------------------------------------------------ remote directory
    def test_ensure_remote_directory_creates_missing_parents(self):
        provider = self._provider()
        sftp = MagicMock()
        sftp.stat.side_effect = FileNotFoundError('no such file')
        provider._ensure_remote_directory(sftp, '/home/backups/deep')
        self.assertEqual(
            [call.args[0] for call in sftp.mkdir.call_args_list],
            ['/home', '/home/backups', '/home/backups/deep'],
        )

    def test_ensure_remote_directory_skips_existing(self):
        provider = self._provider()
        sftp = MagicMock()
        provider._ensure_remote_directory(sftp, '/home/backups')
        sftp.mkdir.assert_not_called()

    def test_ensure_remote_directory_rejects_parent_component(self):
        provider = self._provider()
        with self.assertRaises(UserError):
            provider._ensure_remote_directory(MagicMock(), '/home/../etc')

    # ------------------------------------------------------------ host keys
    def test_pinned_host_key_roundtrip(self):
        provider = self._provider()
        key = paramiko.RSAKey.generate(1024)
        line = '%s %s' % (key.get_name(), key.get_base64())
        parsed = provider._parse_pinned_host_key(line)
        self.assertEqual(parsed.get_name(), key.get_name())
        self.assertEqual(parsed.get_base64(), key.get_base64())

    def test_pinned_host_key_invalid_raises(self):
        provider = self._provider()
        for bad in ('', 'ssh-rsa', 'ssh-unknown-type AAAA'):
            with self.assertRaises(UserError):
                provider._parse_pinned_host_key(bad)

    def test_key_fingerprint_matches_openssh_sha256_format(self):
        provider = self._provider()
        key = paramiko.RSAKey.generate(1024)
        fingerprint = provider._key_fingerprint(key)
        expected = 'SHA256:' + base64.b64encode(
            hashlib.sha256(key.asbytes()).digest()).decode().rstrip('=')
        self.assertEqual(fingerprint, expected)
        self.assertTrue(fingerprint.startswith('SHA256:'))
        self.assertNotIn('=', fingerprint)

    def test_pin_host_key_writes_key_and_fingerprint(self):
        provider = self._provider()
        key = paramiko.RSAKey.generate(1024)
        provider._pin_host_key(key)
        self.assertEqual(
            provider.server_host_key,
            '%s %s' % (key.get_name(), key.get_base64()),
        )
        self.assertEqual(
            provider.host_key_fingerprint, provider._key_fingerprint(key))
        # A second call must not overwrite the pinned key
        other = paramiko.RSAKey.generate(1024)
        provider._pin_host_key(other)
        self.assertTrue(provider.server_host_key.startswith(key.get_name()))

    def test_pin_host_key_ignored_when_verification_disabled(self):
        provider = self._provider(verify_host_key=False)
        provider._pin_host_key(paramiko.RSAKey.generate(1024))
        self.assertFalse(provider.server_host_key)

    def test_pinned_key_configures_reject_policy(self):
        provider = self._provider()
        key = paramiko.RSAKey.generate(1024)
        provider.write({
            'server_host_key': '%s %s' % (key.get_name(), key.get_base64()),
            'host_key_fingerprint': provider._key_fingerprint(key),
        })
        client = MagicMock()
        self.assertIsNone(provider._configure_host_key_policy(client))
        (policy,) = client.set_missing_host_key_policy.call_args.args
        self.assertIsInstance(policy, paramiko.RejectPolicy)
        client.get_host_keys.return_value.add.assert_called_once()

    def test_unknown_key_uses_trust_on_first_use_policy(self):
        provider = self._provider()
        client = MagicMock()
        policy = provider._configure_host_key_policy(client)
        self.assertIsInstance(policy, backup_provider_sftp._CaptureHostKeyPolicy)
        self.assertIsNone(policy.key)

    def test_disabled_verification_uses_auto_add_policy(self):
        provider = self._provider(verify_host_key=False)
        client = MagicMock()
        self.assertIsNone(provider._configure_host_key_policy(client))
        (policy,) = client.set_missing_host_key_policy.call_args.args
        self.assertIsInstance(policy, paramiko.AutoAddPolicy)

    # --------------------------------------------------------- error mapping
    def test_missing_path_error_detection(self):
        provider = self._provider()
        self.assertTrue(
            provider._is_missing_path_error(FileNotFoundError('x')))
        self.assertTrue(
            provider._is_missing_path_error(
                IOError(errno.ENOENT, 'No such file')))
        self.assertTrue(
            provider._is_missing_path_error(IOError('No such file')))
        self.assertFalse(
            provider._is_missing_path_error(IOError(errno.EACCES, 'denied')))

    def test_authentication_error_message(self):
        provider = self._provider()
        message = provider._describe_connection_error(
            paramiko.AuthenticationException('Authentication failed.'))
        self.assertEqual(
            message, 'Authentication failed. Check username/password or key.')

    def test_timeout_error_message(self):
        provider = self._provider()
        message = provider._describe_connection_error(TimeoutError('timed out'))
        self.assertEqual(message, 'Connection timeout. Server may be unreachable.')

    def test_host_key_mismatch_gets_security_warning(self):
        provider = self._provider()
        key = paramiko.RSAKey.generate(1024)
        provider.write({
            'server_host_key': '%s %s' % (key.get_name(), key.get_base64()),
            'host_key_fingerprint': provider._key_fingerprint(key),
        })
        error = paramiko.BadHostKeyException(
            'sftp.example.com', paramiko.RSAKey.generate(1024), key)
        message = provider._describe_connection_error(error)
        self.assertIn('SECURITY WARNING', message)
        self.assertIn(provider.host_key_fingerprint, message)

    # ------------------------------------------------------------ operations
    def test_upload_verification_detects_size_mismatch(self):
        provider = self._provider()
        with tempfile.NamedTemporaryFile(suffix='.zip') as local:
            local.write(b'x' * 32)
            local.flush()
            sftp = MagicMock()
            sftp.stat.return_value = MagicMock(st_size=7)
            with self._use_session(sftp):
                result = provider.upload_backup(local.name, 'db.zip')
        self.assertFalse(result['success'])
        self.assertIn('Upload verification failed', result['message'])
        sftp.put.assert_called_once()

    def test_upload_preserves_permissions_and_reports_metadata(self):
        provider = self._provider()
        with tempfile.NamedTemporaryFile(suffix='.zip') as local:
            local.write(b'y' * 64)
            local.flush()
            os.chmod(local.name, 0o640)
            expected_mode = os.stat(local.name).st_mode
            sftp = MagicMock()
            sftp.stat.return_value = MagicMock(st_size=64, st_mode=0o100640)
            with self._use_session(sftp):
                result = provider.upload_backup(local.name, 'db.zip')
            self.assertTrue(result['success'])
            self.assertEqual(result['metadata']['size_bytes'], 64)
            self.assertEqual(result['metadata']['permissions'], '0o100640')
            self.assertEqual(result['metadata']['remote_path'], '/home/backups/db.zip')
            sftp.chmod.assert_called_once_with('/home/backups/db.zip', expected_mode)
            sftp.put.assert_called_once()

    def test_download_missing_remote_file(self):
        provider = self._provider()
        sftp = MagicMock()
        sftp.stat.side_effect = FileNotFoundError('no such file')
        with self._use_session(sftp):
            result = provider.download_backup('missing.zip', '/tmp/nowhere.zip')
        self.assertFalse(result['success'])
        self.assertIn('Remote backup file not found', result['message'])

    def test_list_backups_missing_directory_returns_empty(self):
        provider = self._provider()
        sftp = MagicMock()
        sftp.listdir.side_effect = FileNotFoundError('no such file')
        with self._use_session(sftp):
            self.assertEqual(provider.list_backups(), [])

    def test_list_backups_filters_and_sorts_newest_first(self):
        provider = self._provider()
        sftp = MagicMock()
        sftp.listdir.return_value = ['old.zip', 'notes.txt', 'new.dump']
        attrs = {
            'old.zip': MagicMock(st_size=10, st_mtime=1000, st_mode=0o100644),
            'new.dump': MagicMock(st_size=20, st_mtime=2000, st_mode=0o100600),
        }
        sftp.stat.side_effect = lambda path: attrs[os.path.basename(path)]
        with self._use_session(sftp):
            backups = provider.list_backups()
            prefixed = provider.list_backups(prefix='new')
        self.assertEqual([b['filename'] for b in backups], ['new.dump', 'old.zip'])
        self.assertEqual([b['filename'] for b in prefixed], ['new.dump'])
        self.assertEqual(backups[0]['size_bytes'], 20)

    def test_delete_missing_file_reports_failure(self):
        provider = self._provider()
        sftp = MagicMock()
        sftp.remove.side_effect = FileNotFoundError('no such file')
        with self._use_session(sftp):
            result = provider.delete_backup('missing.zip')
        self.assertFalse(result['success'])
        self.assertIn('Backup file not found', result['message'])

    def test_delete_existing_file_reports_success(self):
        provider = self._provider()
        with self._use_session(MagicMock()):
            result = provider.delete_backup('db.zip')
        self.assertTrue(result['success'])

    def test_storage_info_parses_df_output(self):
        provider = self._provider()
        sftp = MagicMock()
        sftp.listdir.return_value = ['db.zip']
        sftp.stat.return_value = MagicMock(st_size=1024, st_mtime=1000, st_mode=0o100644)
        client = MagicMock()
        stdout = MagicMock()
        stdout.channel.recv_exit_status.return_value = 0
        stdout.read.return_value = b'/dev/sda1 1000000 250000 750000 25% /home\n'
        client.exec_command.return_value = (MagicMock(), stdout, MagicMock())
        with self._use_session(sftp, client=client):
            info = provider.get_storage_info()
        self.assertEqual(info['backup_count'], 1)
        self.assertEqual(info['total_space_gb'], 1000000 / (1024 ** 3))
        self.assertEqual(info['used_space_gb'], 250000 / (1024 ** 3))
        self.assertEqual(info['free_space_gb'], 750000 / (1024 ** 3))

    def test_storage_info_survives_missing_df(self):
        provider = self._provider()
        sftp = MagicMock()
        sftp.listdir.return_value = []
        client = MagicMock()
        client.exec_command.side_effect = paramiko.SSHException('no shell')
        with self._use_session(sftp, client=client):
            info = provider.get_storage_info()
        self.assertEqual(info['backup_count'], 0)
        self.assertIsNone(info['free_space_gb'])

    # -------------------------------------------------------- session wiring
    def test_transfer_timeout_applied_to_channel(self):
        provider = self._provider(transfer_timeout=77, verify_host_key=False)
        client = MagicMock()
        sftp = MagicMock()
        client.open_sftp.return_value = sftp
        with patch.object(paramiko, 'SSHClient', return_value=client):
            with provider._sftp_session() as session:
                self.assertIs(session.sftp, sftp)
        sftp.get_channel.return_value.settimeout.assert_called_once_with(77)
        client.close.assert_called_once()
        sftp.close.assert_called_once()

    def test_session_pins_captured_host_key(self):
        provider = self._provider()
        client = MagicMock()
        key = paramiko.RSAKey.generate(1024)

        def capture(policy):
            policy.key = key

        client.set_missing_host_key_policy.side_effect = capture
        client.open_sftp.return_value = MagicMock()
        with patch.object(paramiko, 'SSHClient', return_value=client):
            with provider._sftp_session() as session:
                self.assertEqual(session.pinned_fingerprint,
                                 provider._key_fingerprint(key))
        self.assertTrue(provider.server_host_key.startswith(key.get_name()))

    def test_session_cleans_up_private_key_file(self):
        provider = self._provider(
            auth_type='key', password=False,
            private_key='-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n',
        )
        options, cleanup = provider._get_connection_options()
        key_path = options['key_filename']
        self.assertTrue(os.path.exists(key_path))
        self.assertEqual(os.stat(key_path).st_mode & 0o777, 0o600)
        cleanup()
        self.assertFalse(os.path.exists(key_path))

    def test_password_auth_disables_keys_and_agent(self):
        provider = self._provider()
        options, _cleanup = provider._get_connection_options()
        self.assertEqual(options['password'], 'secret')
        self.assertFalse(options['look_for_keys'])
        self.assertFalse(options['allow_agent'])

    def test_default_key_auth_enables_keys_and_agent(self):
        provider = self._provider(auth_type='key', password=False, private_key=False)
        options, _cleanup = provider._get_connection_options()
        self.assertTrue(options['look_for_keys'])
        self.assertTrue(options['allow_agent'])
        self.assertNotIn('key_filename', options)


@tagged('backup_versatile', 'sftp_loopback')
class TestSftpParamikoLoopback(TransactionCase):
    """End-to-end test against a real SFTP server.

    Enable by exporting ``BV_SFTP_TEST_PYTHON`` (interpreter that has
    ``sftpserver`` installed) and optionally ``BV_SFTP_TEST_PORT`` (default
    2222). The harness script (``/tmp/bv_sftp_harness.py``) must already be
    running; without these variables the test is skipped, so the suite stays
    green on machines without the harness.
    """

    def setUp(self):
        super().setUp()
        self.port = int(os.environ.get('BV_SFTP_TEST_PORT', 2222))
        if not os.environ.get('BV_SFTP_TEST_PYTHON'):
            self.skipTest('Loopback SFTP harness not configured')

    def _provider(self, **vals):
        values = {
            'name': 'Loopback SFTP',
            'hostname': '127.0.0.1',
            'port': self.port,
            'username': 'backupuser',
            'auth_type': 'password',
            'password': 'backuppass',
            'remote_directory': '/home/backups/tests',
            'create_remote_dirs': True,
            'verify_host_key': True,
            'connection_timeout': 15,
            'transfer_timeout': 60,
        }
        values.update(vals)
        return self.env['backup.provider.sftp'].create(values)

    def test_full_roundtrip_against_real_server(self):
        provider = self._provider()

        # 1. Connection test pins the host key (trust on first use)
        result = provider.test_connection()
        self.assertTrue(result['success'], result['message'])
        self.assertTrue(provider.host_key_fingerprint)
        self.assertTrue(provider.server_host_key)

        # 2. Upload a real file
        with tempfile.NamedTemporaryFile(suffix='.zip', delete=False) as local:
            local.write(os.urandom(2048))
            local_path = local.name
        try:
            upload = provider.upload_backup(local_path, 'e2e_test.zip')
            self.assertTrue(upload['success'], upload.get('message'))
            self.assertEqual(upload['metadata']['size_bytes'], 2048)

            # 3. It shows up in the listing
            names = [b['filename'] for b in provider.list_backups()]
            self.assertIn('e2e_test.zip', names)

            # 4. Download and compare content
            download_path = local_path + '.back'
            download = provider.download_backup('e2e_test.zip', download_path)
            self.assertTrue(download['success'], download.get('message'))
            with open(local_path, 'rb') as fh:
                expected = hashlib.sha256(fh.read()).hexdigest()
            with open(download_path, 'rb') as fh:
                actual = hashlib.sha256(fh.read()).hexdigest()
            self.assertEqual(expected, actual)

            # 5. Cleanup
            deleted = provider.delete_backup('e2e_test.zip')
            self.assertTrue(deleted['success'], deleted.get('message'))
            self.assertNotIn(
                'e2e_test.zip', [b['filename'] for b in provider.list_backups()])
        finally:
            if os.path.exists(local_path):
                os.unlink(local_path)
            if os.path.exists(local_path + '.back'):
                os.unlink(local_path + '.back')

    def test_host_key_mismatch_is_rejected(self):
        """A server presenting a different key must be refused."""
        provider = self._provider()
        wrong = paramiko.RSAKey.generate(1024)
        provider.write({
            'server_host_key': '%s %s' % (wrong.get_name(), wrong.get_base64()),
            'host_key_fingerprint': provider._key_fingerprint(wrong),
        })
        result = provider.test_connection()
        self.assertFalse(result['success'])
        self.assertIn('SECURITY WARNING', result['message'])
