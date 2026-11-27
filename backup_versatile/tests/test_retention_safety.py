import datetime
import json
from types import SimpleNamespace
from unittest.mock import patch

from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase, tagged


@tagged('backup_versatile', 'standard')
class TestRetentionSafety(TransactionCase):
    """Regression coverage for ownership-safe backup retention."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Config = cls.env['backup.config']
        cls.Job = cls.env['backup.job']
        cls.LocalProvider = cls.env['backup.provider.local']

    def _provider(self, **vals):
        values = {
            'name': 'Retention local provider',
            'active': True,
            'backup_directory': '/tmp/bv_retention_owned',
            'create_subdirectories': False,
            'check_disk_space': False,
        }
        values.update(vals)
        return self.LocalProvider.create(values)

    def _config(self, providers=(), **vals):
        values = {
            'name': 'Retention config',
            'database_name': 'owned_db',
            'active': False,
            'retention_policy': 'count',
            'retention_count': 1,
            'job_retention_days': 0,
        }
        if providers:
            values['local_provider_ids'] = [(6, 0, providers.ids)]
        values.update(vals)
        return self.Config.create(values)

    def _job(self, config, filename, provider=None, status='success', **vals):
        provider_results = vals.pop('provider_results', None)
        if provider_results is None and provider is not None:
            provider_results = {
                provider._provider_result_key(): {'success': True, 'metadata': {}},
            }
        values = {
            'config_id': config.id,
            'database_name': config.database_name,
            'backup_format': 'zip',
            'backup_filename': filename,
            'status': status,
            'provider_results': (
                json.dumps(provider_results) if provider_results is not None else False
            ),
        }
        values.update(vals)
        return self.Job.create(values)

    def _listing(self, filename, days=0, full_path=None):
        return {
            'filename': filename,
            'full_path': full_path or '/tmp/bv_retention_owned/%s' % filename,
            'size_bytes': 10,
            'created_date': datetime.datetime.now() - datetime.timedelta(days=days),
            'modified_date': datetime.datetime.now() - datetime.timedelta(days=days),
        }

    def test_scheduler_cron_is_enabled_by_default(self):
        cron = self.env.ref('backup_versatile.backup_cron')
        self.assertTrue(cron.active)
        self.assertEqual(cron.code, 'model.run_scheduled_backups()')

    def test_scheduler_is_safe_without_active_configs(self):
        with patch.object(type(self.Config), 'search', return_value=self.Config.browse()), \
                patch.object(type(self.Config), 'cleanup_old_backups') as cleanup:
            self.assertIsNone(self.Config.run_scheduled_backups())
        cleanup.assert_not_called()

    def test_scheduled_retention_runs_only_after_success(self):
        provider = self._provider()
        config = self._config(provider, active=True)

        for status in ('success', 'warning', 'error'):
            with self.subTest(status=status):
                job = SimpleNamespace(status=status)
                with patch.object(type(self.Config), 'search', return_value=config), \
                        patch.object(type(self.Config), 'create_backup', return_value=job), \
                        patch.object(type(self.Config), 'cleanup_old_backups') as cleanup:
                    self.Config.run_scheduled_backups()
                if status == 'success':
                    cleanup.assert_called_once()
                else:
                    cleanup.assert_not_called()

    def test_scheduler_skips_config_without_active_providers(self):
        provider = self._provider()
        config = self._config(provider, active=True)
        provider.active = False

        with patch.object(type(self.Config), 'search', return_value=config), \
                patch.object(type(self.Config), 'create_backup') as create_backup, \
                patch.object(type(self.Config), 'cleanup_old_backups') as cleanup:
            self.Config.run_scheduled_backups()
        create_backup.assert_not_called()
        cleanup.assert_not_called()

    def test_cleanup_deletes_only_exact_owned_successful_backups(self):
        provider = self._provider()
        other_provider = self._provider(name='Other provider')
        config = self._config(provider)

        self._job(config, 'owned_old.zip', provider)
        self._job(config, 'owned_new.zip', provider)
        self._job(config, 'failed_job.zip', provider, status='error')
        self._job(config, 'warning_job.zip', provider, status='warning')
        self._job(config, 'failed_provider.zip', provider_results={
            provider._provider_result_key(): {'success': False},
            provider.name: {'success': True},
        })
        self._job(config, 'other_provider.zip', other_provider)
        self._job(config, 'missing_results.zip')
        malformed_job = self._job(config, 'malformed_results.zip')
        malformed_job.provider_results = '{not-json'
        self._job(config, 'duplicate.zip', provider)

        listings = [
            self._listing('owned_new.zip', days=1),
            self._listing('owned_old.zip', days=2),
            self._listing('owned_db_rogue.zip', days=90),
            self._listing('failed_job.zip', days=90),
            self._listing('warning_job.zip', days=90),
            self._listing('failed_provider.zip', days=90),
            self._listing('other_provider.zip', days=90),
            self._listing('missing_results.zip', days=90),
            self._listing('malformed_results.zip', days=90),
            self._listing('duplicate.zip', days=90, full_path='/tmp/bv_retention_owned/a/duplicate.zip'),
            self._listing('duplicate.zip', days=90, full_path='/tmp/bv_retention_owned/b/duplicate.zip'),
        ]
        with patch.object(type(provider), 'list_backups', return_value=listings), \
                patch.object(type(provider), 'delete_backup', return_value={'success': True}) as delete:
            config.cleanup_old_backups()

        delete.assert_called_once_with('/tmp/bv_retention_owned/owned_old.zip')

    def test_legacy_unique_provider_name_is_accepted_conservatively(self):
        provider = self._provider()
        config = self._config(provider)
        self._job(config, 'legacy_old.zip', provider_results={
            provider.name: {'success': True, 'metadata': {}},
        })
        self._job(config, 'legacy_new.zip', provider_results={
            provider.name: {'success': True, 'metadata': {}},
        })
        listings = [
            self._listing('legacy_new.zip', days=1),
            self._listing('legacy_old.zip', days=2),
        ]
        with patch.object(type(provider), 'list_backups', return_value=listings), \
                patch.object(type(provider), 'delete_backup', return_value={'success': True}) as delete:
            config.cleanup_old_backups()
        delete.assert_called_once_with('/tmp/bv_retention_owned/legacy_old.zip')

    def test_ambiguous_legacy_provider_name_is_not_deleted(self):
        first = self._provider(name='Duplicate provider')
        second = self._provider(name='Duplicate provider')
        config = self._config(first + second)
        self._job(config, 'ambiguous.zip', provider_results={
            first.name: {'success': True, 'metadata': {}},
        })
        listings = [self._listing('ambiguous.zip', days=90)]
        with patch.object(type(first), 'list_backups', return_value=listings), \
                patch.object(type(second), 'list_backups', return_value=listings), \
                patch.object(type(first), 'delete_backup') as first_delete, \
                patch.object(type(second), 'delete_backup') as second_delete:
            config.cleanup_old_backups()
        first_delete.assert_not_called()
        second_delete.assert_not_called()

    def test_last_verified_copy_is_preserved(self):
        provider = self._provider()
        config = self._config(
            provider, retention_policy='days', retention_days=1)
        self._job(config, 'old_newest.zip', provider)
        self._job(config, 'old_older.zip', provider)
        listings = [
            self._listing('old_newest.zip', days=60),
            self._listing('old_older.zip', days=90),
        ]
        with patch.object(type(provider), 'list_backups', return_value=listings), \
                patch.object(type(provider), 'delete_backup', return_value={'success': True}) as delete:
            config.cleanup_old_backups()
        delete.assert_called_once_with('/tmp/bv_retention_owned/old_older.zip')

        only_old = [self._listing('only_old.zip', days=90)]
        self.assertEqual(config._get_backups_to_delete(only_old), [])

    def test_inactive_providers_are_filtered_server_side(self):
        active = self._provider(name='Active provider')
        inactive = self._provider(name='Inactive provider', active=False)
        config = self._config(active + inactive)
        self.assertEqual(config.all_providers, [active])

        with self.assertRaises(ValidationError):
            self._config(inactive, active=True)

    def test_active_config_cannot_be_left_with_only_inactive_providers(self):
        active = self._provider(name='Active provider')
        inactive = self._provider(name='Inactive provider', active=False)
        config = self._config(active, active=True)
        with self.assertRaises(ValidationError):
            config.local_provider_ids = [(6, 0, inactive.ids)]

    def test_provider_constraint_covers_every_many2many_field(self):
        sftp = self.env['backup.provider.sftp'].create({
            'name': 'Inactive SFTP provider',
            'active': False,
            'hostname': 'sftp.example.com',
            'username': 'backupuser',
            'remote_directory': '/backups',
        })
        cloud = self.env['backup.provider.cloud'].create({
            'name': 'Inactive cloud provider',
            'active': False,
            'bucket': 'backups',
        })
        cases = (
            (self._provider(name='Inactive local provider', active=False),
             'local_provider_ids'),
            (sftp, 'sftp_provider_ids'),
            (cloud, 'cloud_provider_ids'),
        )
        for provider, field_name in cases:
            with self.subTest(field=field_name):
                config = self._config()
                with self.assertRaises(ValidationError):
                    config.write({
                        'active': True,
                        field_name: [(4, provider.id)],
                    })

    def test_job_retention_days_constraint(self):
        with self.assertRaises(ValidationError):
            self._config(job_retention_days=-1)
        config = self._config(job_retention_days=0)
        self.assertEqual(config.job_retention_days, 0)

    def test_warning_status_and_counter_are_preserved(self):
        config = self._config()
        config.total_backups_created = 4
        job = self._job(config, 'partial.zip', status='warning')

        config._update_backup_statistics({
            'success': True,
            'message': 'Only 1/2 provider uploads succeeded',
            'backup_job': job,
        })

        self.assertEqual(config.last_backup_status, 'warning')
        self.assertEqual(config.total_backups_created, 5)
        self.assertEqual(
            config.last_backup_message, 'Only 1/2 provider uploads succeeded')

    def test_new_jobs_key_provider_results_by_model_and_id(self):
        provider = self._provider()
        config = self._config(provider)
        job = self.Job.create({
            'config_id': config.id,
            'database_name': config.database_name,
            'backup_format': 'zip',
            'backup_filename': 'owned_db.zip',
            'status': 'running',
        })
        with patch.object(
            type(provider), 'upload_backup',
            return_value={
                'success': True,
                'metadata': {
                    'file_path': '/tmp/bv_retention_owned/owned_db.zip',
                    'size_bytes': 10,
                },
            },
        ):
            results = job._upload_to_providers_sequential('/tmp/source.zip', [provider])
        self.assertEqual(
            list(results), ['backup.provider.local,%s' % provider.id])
        self.assertTrue(results[provider._provider_result_key()]['success'])
        artifact = self.env['backup.artifact'].browse(
            results[provider._provider_result_key()]['artifact_id']
        )
        self.assertEqual(artifact.config_id, config)
        self.assertEqual(artifact.provider_id, provider.id)
        self.assertEqual(artifact.filename, 'owned_db.zip')

    def test_artifact_ownership_survives_job_history_pruning(self):
        provider = self._provider()
        config = self._config(provider)
        job = self._job(config, 'artifact_old.zip', provider)
        artifact = self.env['backup.artifact'].create({
            'job_id': job.id,
            'config_id': config.id,
            'provider_model': provider._name,
            'provider_id': provider.id,
            'filename': 'artifact_old.zip',
            'object_path': '/tmp/bv_retention_owned/artifact_old.zip',
        })
        self._job(config, 'artifact_new.zip', provider)
        job.unlink()
        listings = [
            self._listing('artifact_new.zip', days=1),
            self._listing('artifact_old.zip', days=90),
        ]
        with patch.object(type(provider), 'list_backups', return_value=listings), \
                patch.object(type(provider), 'delete_backup', return_value={'success': True}) as delete:
            config.cleanup_old_backups()
        delete.assert_called_once_with('/tmp/bv_retention_owned/artifact_old.zip')
        self.assertFalse(artifact.active)
        self.assertTrue(artifact.deleted_date)

    def test_deleted_artifact_is_not_resurrected_by_legacy_job_fallback(self):
        provider = self._provider()
        config = self._config(provider)
        job = self._job(config, 'deleted_artifact.zip', provider)
        self.env['backup.artifact'].create({
            'job_id': job.id,
            'config_id': config.id,
            'provider_model': provider._name,
            'provider_id': provider.id,
            'filename': 'deleted_artifact.zip',
            'object_path': '/tmp/bv_retention_owned/deleted_artifact.zip',
            'active': False,
        })
        job.provider_results = json.dumps({
            provider._provider_result_key(): {
                'success': True,
                'artifact_id': 999999,
            },
        })
        listings = [self._listing('deleted_artifact.zip', days=90)]
        with patch.object(type(provider), 'list_backups', return_value=listings), \
                patch.object(type(provider), 'delete_backup') as delete:
            config.cleanup_old_backups()
        delete.assert_not_called()
