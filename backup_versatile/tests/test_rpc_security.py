from unittest.mock import patch

from odoo.exceptions import AccessDenied, AccessError
from odoo.service.model import get_public_method
from odoo.tests.common import TransactionCase, tagged


@tagged('backup_versatile', 'standard')
class TestBackupRpcSecurity(TransactionCase):
    """Read-only backup users must not trigger provider side effects."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        company = cls.env.ref('base.main_company')
        cls.limited_user = cls.env['res.users'].create({
            'name': 'Backup read-only user',
            'login': 'backup_read_only_user',
            'company_id': company.id,
            'company_ids': [(6, 0, company.ids)],
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref('backup_versatile.group_backup_user').id,
            ])],
        })
        cls.Config = cls.env['backup.config']
        cls.Provider = cls.env['backup.provider.local']
        cls.Job = cls.env['backup.job']

    def test_provider_operations_are_not_rpc_callable(self):
        operations = (
            'test_connection',
            'upload_backup',
            'download_backup',
            'list_backups',
            'delete_backup',
            'get_storage_info',
        )
        for model_name in (
            'backup.provider.local',
            'backup.provider.sftp',
            'backup.provider.cloud',
        ):
            model = self.env[model_name]
            for operation in operations:
                with self.subTest(model=model_name, operation=operation):
                    with self.assertRaises(AccessError):
                        get_public_method(model, operation)

    def test_read_only_user_cannot_run_config_actions(self):
        config = self.Config.new({
            'name': 'Read-only action test',
            'database_name': self.env.cr.dbname,
        }).with_user(self.limited_user)
        for action in (
            'create_backup',
            'test_providers',
            'action_generate_encryption_key',
            'test_retention_policy',
            'cleanup_old_backups',
        ):
            with self.subTest(action=action):
                with self.assertRaises(AccessDenied):
                    getattr(config, action)()

        with self.assertRaises(AccessDenied):
            self.Config.with_user(self.limited_user).run_scheduled_backups()

    def test_read_only_user_cannot_retry_a_backup(self):
        job = self.Job.new({
            'database_name': self.env.cr.dbname,
            'status': 'error',
        }).with_user(self.limited_user)
        with self.assertRaises(AccessDenied):
            job.retry_backup()

    def test_read_only_user_cannot_test_a_provider(self):
        provider = self.Provider.new({'name': 'Read-only provider test'})
        with self.assertRaises(AccessDenied):
            provider.with_user(self.limited_user).test_connection_action()

    def test_admin_can_call_private_provider_operations_internally(self):
        provider = self.Provider.new({'name': 'Admin provider test'})
        with patch.object(
            type(provider),
            'test_connection',
            lambda self: {'success': True, 'message': 'Connection OK'},
        ):
            action = provider.test_connection_action()
        self.assertEqual(action['params']['type'], 'success')

    def test_admin_can_run_the_scheduled_entry_point(self):
        with patch.object(type(self.Config), 'search', return_value=self.Config):
            self.assertIsNone(self.Config.run_scheduled_backups())
