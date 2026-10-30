import datetime

from odoo.tests.common import TransactionCase, tagged


@tagged('backup_versatile', 'standard')
class TestRetentionPolicy(TransactionCase):

    def _config(self, **kw):
        values = {
            'name': 'Test Config',
            'database_name': 'test_db',
            'active': False,
            'retention_policy': 'count',
            'retention_count': 3,
        }
        values.update(kw)
        return self.env['backup.config'].create(values)

    def _now(self):
        return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)

    def test_normalize_dt_none(self):
        Config = self.env['backup.config']
        self.assertFalse(Config._normalize_dt(None))

    def test_normalize_dt_naive_passthrough(self):
        Config = self.env['backup.config']
        naive = datetime.datetime(2026, 1, 1, 12, 0, 0)
        self.assertEqual(Config._normalize_dt(naive), naive)
        self.assertIsNone(Config._normalize_dt(naive).tzinfo)

    def test_normalize_dt_aware_to_naive_utc(self):
        Config = self.env['backup.config']
        aware = datetime.datetime(
            2026, 1, 1, 15, 0, 0,
            tzinfo=datetime.timezone(datetime.timedelta(hours=2)),
        )
        result = Config._normalize_dt(aware)
        self.assertIsNone(result.tzinfo)
        self.assertEqual(result, datetime.datetime(2026, 1, 1, 13, 0, 0))

    def test_count_policy_keeps_last_n(self):
        config = self._config(retention_policy='count', retention_count=2)
        now = self._now()
        backups = [
            {'filename': 'b%d.zip' % i, 'modified_date': now - datetime.timedelta(days=i)}
            for i in range(5)
        ]
        to_delete = config._get_backups_to_delete(backups)
        self.assertEqual(len(to_delete), 3)

    def test_count_policy_below_threshold(self):
        config = self._config(retention_policy='count', retention_count=10)
        backups = [{'filename': 'only.zip', 'modified_date': self._now()}]
        self.assertEqual(config._get_backups_to_delete(backups), [])

    def test_count_policy_empty(self):
        config = self._config()
        self.assertEqual(config._get_backups_to_delete([]), [])

    def test_days_policy_deletes_old(self):
        config = self._config(retention_policy='days', retention_days=30)
        now = self._now()
        backups = [
            {'filename': 'recent.zip', 'modified_date': now - datetime.timedelta(days=5)},
            {'filename': 'old.zip', 'modified_date': now - datetime.timedelta(days=60)},
        ]
        to_delete = config._get_backups_to_delete(backups)
        self.assertEqual([b['filename'] for b in to_delete], ['old.zip'])

    def test_custom_policy_is_union(self):
        config = self._config(
            retention_policy='custom', retention_count=2, retention_days=30)
        now = self._now()
        backups = [
            {'filename': 'new1.zip', 'modified_date': now - datetime.timedelta(days=1)},
            {'filename': 'new2.zip', 'modified_date': now - datetime.timedelta(days=2)},
            {'filename': 'mid4.zip', 'modified_date': now - datetime.timedelta(days=5)},
            {'filename': 'old3.zip', 'modified_date': now - datetime.timedelta(days=60)},
        ]
        names = sorted(b['filename'] for b in config._get_backups_to_delete(backups))
        # beyond last 2 -> mid4, old3 ; older than 30 days -> old3
        self.assertEqual(names, ['mid4.zip', 'old3.zip'])

    def test_days_policy_mixed_tz_aware_and_naive(self):
        """Regression (P0 #2): cloud providers return tz-aware datetimes while
        local/sftp return naive. Comparing them used to raise TypeError,
        silently swallowed by the cleanup cron so retention never ran."""
        config = self._config(retention_policy='days', retention_days=30)
        naive_now = datetime.datetime.now()
        aware_now = datetime.datetime.now(datetime.timezone.utc)
        backups = [
            {'filename': 'naive_recent.zip', 'modified_date': naive_now},
            {'filename': 'aware_recent.zip', 'modified_date': aware_now},
            {'filename': 'naive_old.zip',
             'modified_date': naive_now - datetime.timedelta(days=60)},
            {'filename': 'aware_old.zip',
             'modified_date': aware_now - datetime.timedelta(days=60)},
        ]
        to_delete = config._get_backups_to_delete(backups)
        names = sorted(b['filename'] for b in to_delete)
        self.assertEqual(names, ['aware_old.zip', 'naive_old.zip'])
