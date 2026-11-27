from odoo.exceptions import AccessDenied
from odoo.tests.common import TransactionCase, tagged


@tagged('backup_versatile', 'standard')
class TestBackupAuthorizationScope(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        company = cls.env.ref('base.main_company')
        cls.backup_admin = cls.env['res.users'].create({
            'name': 'Limited backup administrator',
            'login': 'limited_backup_admin',
            'company_id': company.id,
            'company_ids': [(6, 0, company.ids)],
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref('backup_versatile.group_backup_admin').id,
            ])],
        })

    def _job(self, database_name):
        config = self.env['backup.config'].new({
            'name': 'Authorization scope',
            'database_name': database_name,
        })
        return self.env['backup.job'].new({
            'config_id': config,
            'database_name': database_name,
            'backup_format': 'zip',
        }).with_user(self.backup_admin)

    def test_cross_database_backup_requires_system_admin(self):
        with self.assertRaises(AccessDenied):
            self._job('postgres')._preflight_database()

    def test_current_database_backup_remains_available(self):
        self.assertTrue(self._job(self.env.cr.dbname)._preflight_database())

    def test_restore_requires_system_admin_before_provider_access(self):
        wizard = self.env['backup.restore.wizard'].new({
            'provider_type': 'local',
            'selected_backup': 'backup.zip',
            'target_database': 'restore_target',
            'create_database': True,
        }).with_user(self.backup_admin)
        with self.assertRaises(AccessDenied):
            wizard.action_restore()

    def test_restore_menu_is_system_admin_only(self):
        menu = self.env.ref('backup_versatile.menu_restore_backup')
        self.assertEqual(menu.group_ids, self.env.ref('base.group_system'))
