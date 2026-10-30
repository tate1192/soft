from odoo.tests.common import TransactionCase, tagged


@tagged('backup_versatile', 'standard')
class TestBackupFileFilter(TransactionCase):

    def setUp(self):
        super().setUp()
        self.provider = self.env['backup.provider.local'].new({
            'name': 'local',
            'backup_directory': '/tmp',
        })

    def test_accepts_standard_extensions(self):
        for ext in ('.zip', '.dump', '.sql', '.tar.gz', '.tgz'):
            self.assertTrue(self.provider._is_backup_file('backup' + ext), ext)

    def test_rejects_non_backup(self):
        for name in ('readme.txt', 'image.png', 'doc.pdf', 'data.json', 'noext'):
            self.assertFalse(self.provider._is_backup_file(name), name)

    def test_case_insensitive(self):
        self.assertTrue(self.provider._is_backup_file('BACKUP.ZIP'))
        self.assertTrue(self.provider._is_backup_file('Backup.DUMP'))
