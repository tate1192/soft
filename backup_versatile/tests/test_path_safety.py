from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged


@tagged('backup_versatile', 'standard')
class TestPathSafety(TransactionCase):

    def test_sftp_normal_filename(self):
        Sftp = self.env['backup.provider.sftp']
        self.assertEqual(Sftp._sanitize_remote_filename('backup.zip'), 'backup.zip')

    def test_sftp_directory_stripped(self):
        Sftp = self.env['backup.provider.sftp']
        self.assertEqual(Sftp._sanitize_remote_filename('dir/sub/backup.zip'), 'backup.zip')

    def test_sftp_traversal_neutralized(self):
        """Regression (P0 #4): '../../etc/passwd' is reduced to its basename so
        no directory traversal is possible on the remote host."""
        Sftp = self.env['backup.provider.sftp']
        self.assertEqual(Sftp._sanitize_remote_filename('../../etc/passwd'), 'passwd')

    def test_sftp_dotdot_rejected(self):
        Sftp = self.env['backup.provider.sftp']
        with self.assertRaises(UserError):
            Sftp._sanitize_remote_filename('..')

    def test_sftp_dot_rejected(self):
        Sftp = self.env['backup.provider.sftp']
        with self.assertRaises(UserError):
            Sftp._sanitize_remote_filename('.')

    def test_sftp_trailing_slash_rejected(self):
        Sftp = self.env['backup.provider.sftp']
        with self.assertRaises(UserError):
            Sftp._sanitize_remote_filename('../')

    def test_sftp_null_byte_rejected(self):
        Sftp = self.env['backup.provider.sftp']
        with self.assertRaises(UserError):
            Sftp._sanitize_remote_filename('back\x00up.zip')

    def test_sftp_empty_passthrough(self):
        Sftp = self.env['backup.provider.sftp']
        self.assertFalse(Sftp._sanitize_remote_filename(''))

    def test_local_normal_filename(self):
        Local = self.env['backup.provider.local']
        self.assertEqual(Local._sanitize_remote_filename('backup.zip'), 'backup.zip')

    def test_local_traversal_neutralized(self):
        """Regression: local provider must sanitize filenames like SFTP/cloud."""
        Local = self.env['backup.provider.local']
        self.assertEqual(Local._sanitize_remote_filename('../../etc/passwd'), 'passwd')

    def test_local_dotdot_rejected(self):
        Local = self.env['backup.provider.local']
        with self.assertRaises(UserError):
            Local._sanitize_remote_filename('..')

    def test_local_null_byte_rejected(self):
        Local = self.env['backup.provider.local']
        with self.assertRaises(UserError):
            Local._sanitize_remote_filename('back\x00up.zip')

    def _cloud(self, **kw):
        values = {'name': 'cloud', 'cloud_service': 's3', 'bucket': 'mybucket'}
        values.update(kw)
        return self.env['backup.provider.cloud'].new(values)

    def test_cloud_remote_path_normal(self):
        self.assertEqual(
            self._cloud()._remote_path('backup.zip'),
            's3:mybucket/backup.zip',
        )

    def test_cloud_remote_path_traversal_neutralized(self):
        """Regression (P0 #4): cloud remote path strips traversal to basename."""
        self.assertEqual(
            self._cloud()._remote_path('../../etc/passwd'),
            's3:mybucket/passwd',
        )

    def test_cloud_remote_path_dotdot_rejected(self):
        with self.assertRaises(UserError):
            self._cloud()._remote_path('..')
