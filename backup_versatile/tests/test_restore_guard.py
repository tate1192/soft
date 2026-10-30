from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged


@tagged('backup_versatile', 'standard')
class TestRestoreGuard(TransactionCase):

    def _wizard(self, **kw):
        values = {
            'provider_type': 'local',
            'selected_backup': 'backup.zip',
            'target_database': 'test_db',
            'create_database': True,
            'drop_existing': False,
        }
        values.update(kw)
        return self.env['backup.restore.wizard'].new(values)

    def test_drop_without_create_rejected(self):
        """Regression: dropping without recreating would lose the database."""
        wizard = self._wizard(drop_existing=True, create_database=False)
        with self.assertRaises(UserError):
            wizard._validate_restore_options()

    def test_drop_with_create_allowed(self):
        wizard = self._wizard(drop_existing=True, create_database=True)
        wizard._validate_restore_options()

    def test_sanitize_traversal(self):
        Wizard = self.env['backup.restore.wizard']
        self.assertEqual(
            Wizard._sanitize_backup_filename('../../etc/passwd'), 'passwd')

    def test_sanitize_dotdot_rejected(self):
        Wizard = self.env['backup.restore.wizard']
        with self.assertRaises(UserError):
            Wizard._sanitize_backup_filename('..')

    def test_sanitize_null_byte_rejected(self):
        Wizard = self.env['backup.restore.wizard']
        with self.assertRaises(UserError):
            Wizard._sanitize_backup_filename('b\x00ackup.zip')
