import errno
import json
import os
import shutil
import stat
import subprocess
import tempfile
import zipfile
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.backup_versatile.wizard import backup_restore_wizard as restore_module


@tagged('backup_versatile', 'standard')
class TestRestoreSafety(TransactionCase):
    """Regression coverage for validation, staging and rollback ordering."""

    def setUp(self):
        super().setUp()
        self.temp_dir = tempfile.mkdtemp(prefix='bv_restore_safety_')
        self.addCleanup(shutil.rmtree, self.temp_dir, ignore_errors=True)
        self.target = 'bv_restore_target'
        self.target_filestore = os.path.join(
            self.temp_dir, 'data', 'filestore', self.target,
        )

    def _wizard(self, **values):
        defaults = {
            'provider_type': 'local',
            'selected_backup': 'backup.zip',
            'target_database': self.target,
            'create_database': True,
            'drop_existing': True,
            'neutralize_database': False,
        }
        defaults.update(values)
        return self.env['backup.restore.wizard'].new(defaults)

    def _write_zip(self, entries=None, db_name='source_db', corrupt=False):
        path = os.path.join(self.temp_dir, 'backup.zip')
        entries = entries or {
            'dump.sql': b'-- valid dump\n',
            'manifest.json': json.dumps({'db_name': db_name}).encode(),
        }
        with zipfile.ZipFile(path, 'w', zipfile.ZIP_STORED) as zf:
            for name, content in entries.items():
                zf.writestr(name, content)
        if corrupt:
            with zipfile.ZipFile(path) as zf:
                info = zf.getinfo('dump.sql')
                data_offset = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
            with open(path, 'r+b') as backup:
                backup.seek(data_offset)
                original = backup.read(1)
                backup.seek(data_offset)
                backup.write(bytes([original[0] ^ 0xFF]))
        return path

    def _provider(self, backup_path):
        def download(_filename, destination):
            shutil.copyfile(backup_path, destination)
            return {'success': True}

        return SimpleNamespace(name='test provider', download_backup=download)

    @staticmethod
    def _completed(stdout=''):
        return subprocess.CompletedProcess([], 0, stdout=stdout, stderr='')

    def _filestore_patch(self):
        return patch.object(
            restore_module.config, 'filestore', return_value=self.target_filestore,
        )

    def test_reserved_postgresql_targets_are_rejected(self):
        for target in ('postgres', 'template0', 'template1'):
            wizard = self._wizard(target_database=target)
            with self.assertRaisesRegex(UserError, 'reserved by PostgreSQL'):
                wizard._validate_restore_options()

    def test_backup_listing_does_not_truncate_long_filenames(self):
        wizard = self._wizard()
        filename = 'database_' + 'x' * 90 + '.zip'
        provider = SimpleNamespace(
            name='test provider',
            list_backups=lambda: [{
                'filename': filename,
                'size_gb': 1.0,
                'modified_date': datetime(2026, 1, 1),
            }],
        )
        with patch.object(type(wizard), '_get_provider', return_value=provider):
            wizard.action_list_backups()
        self.assertIn(filename, wizard.available_backups)

    def test_crc_failure_precedes_database_and_drop_operations(self):
        wizard = self._wizard()
        backup = self._write_zip(corrupt=True)
        with patch.object(type(wizard), '_get_provider', return_value=self._provider(backup)), \
                patch.object(type(wizard), '_database_exists') as exists, \
                patch.object(type(wizard), '_create_database') as create, \
                patch.object(type(wizard), '_drop_database') as drop, \
                patch.object(type(wizard), '_run_pg_tool') as run_tool:
            with self.assertRaisesRegex(UserError, 'Corrupt entry|valid ZIP'):
                wizard.action_restore()
        exists.assert_not_called()
        create.assert_not_called()
        drop.assert_not_called()
        run_tool.assert_not_called()

    def test_custom_dump_list_failure_precedes_target_operations(self):
        wizard = self._wizard(selected_backup='backup.dump')
        backup = os.path.join(self.temp_dir, 'backup.dump')
        with open(backup, 'wb') as dump:
            dump.write(b'PGDMPnot-a-complete-archive')
        with patch.object(type(wizard), '_get_provider', return_value=self._provider(backup)), \
                patch.object(type(wizard), '_database_exists') as exists, \
                patch.object(type(wizard), '_create_database') as create, \
                patch.object(type(wizard), '_drop_database') as drop, \
                patch.object(type(wizard), '_run_pg_tool', side_effect=UserError('bad archive')) as run_tool:
            with self.assertRaisesRegex(UserError, 'bad archive'):
                wizard.action_restore()
        self.assertEqual(run_tool.call_args.args[0], 'pg_restore')
        self.assertIn('--list', run_tool.call_args.args[1])
        exists.assert_not_called()
        create.assert_not_called()
        drop.assert_not_called()

    def test_zip_requires_dump_and_manifest(self):
        wizard = self._wizard()
        for missing in ('dump.sql', 'manifest.json'):
            path = os.path.join(self.temp_dir, missing + '.zip')
            entries = {
                'dump.sql': b'-- dump\n',
                'manifest.json': b'{"db_name": "source_db"}',
            }
            del entries[missing]
            with zipfile.ZipFile(path, 'w') as zf:
                for name, content in entries.items():
                    zf.writestr(name, content)
                with self.assertRaisesRegex(UserError, missing):
                    wizard._validate_zip_archive(zf)

    def test_zip_manifest_requires_valid_db_name(self):
        wizard = self._wizard()
        for manifest in ({}, {'db_name': ''}, {'db_name': '../unsafe'}, {'db_name': 42}):
            path = os.path.join(self.temp_dir, 'manifest.zip')
            with zipfile.ZipFile(path, 'w') as zf:
                zf.writestr('dump.sql', b'-- dump\n')
                zf.writestr('manifest.json', json.dumps(manifest))
                with self.assertRaisesRegex(UserError, 'Invalid database name'):
                    wizard._validate_zip_archive(zf)

    def test_zip_rejects_traversal_and_symlinks(self):
        wizard = self._wizard()
        path = os.path.join(self.temp_dir, 'unsafe.zip')
        with zipfile.ZipFile(path, 'w') as zf:
            zf.writestr('dump.sql', b'-- dump\n')
            zf.writestr('manifest.json', b'{"db_name": "source_db"}')
            zf.writestr('filestore/../outside', b'bad')
            with self.assertRaisesRegex(UserError, 'unsafe ZIP member path'):
                wizard._validate_zip_archive(zf)

        link = zipfile.ZipInfo('filestore/link')
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(path, 'w') as zf:
            zf.writestr('dump.sql', b'-- dump\n')
            zf.writestr('manifest.json', b'{"db_name": "source_db"}')
            zf.writestr(link, '/etc/passwd')
            with self.assertRaisesRegex(UserError, 'non-regular ZIP member'):
                wizard._validate_zip_archive(zf)

    def test_zip_resource_limits_are_enforced(self):
        wizard = self._wizard()
        path = os.path.join(self.temp_dir, 'large.zip')
        with zipfile.ZipFile(path, 'w') as zf:
            zf.writestr('dump.sql', b'0123456789')
            zf.writestr('manifest.json', b'{"db_name": "source_db"}')
            with patch.object(type(wizard), '_MAX_ZIP_MEMBER_BYTES', 5):
                with self.assertRaisesRegex(UserError, 'per-file size limit'):
                    wizard._validate_zip_archive(zf)

    def test_filestore_root_directory_entry_is_supported(self):
        wizard = self._wizard()
        backup = self._write_zip({
            'dump.sql': b'-- dump\n',
            'manifest.json': b'{"db_name": "source_db"}',
            'filestore/': b'',
        })
        with self._filestore_patch():
            restore = wizard._prepare_restore(backup, self.temp_dir)
        self.assertTrue(os.path.isdir(restore['filestore']))
        shutil.rmtree(restore['filestore'])

    def test_psql_restore_uses_random_restrict_token_and_X(self):
        wizard = self._wizard()
        restore = {'method': 'psql', 'source': '/tmp/dump.sql'}
        with patch.object(restore_module.secrets, 'token_hex', side_effect=['abc123', 'def456']) as token, \
                patch.object(type(wizard), '_run_pg_tool') as run_tool:
            wizard._restore_staging_database('stage_one', restore)
            wizard._restore_staging_database('stage_two', restore)
        self.assertEqual(token.call_count, 2)
        first_args = run_tool.call_args_list[0].args[1]
        second_args = run_tool.call_args_list[1].args[1]
        self.assertIn('-X', first_args)
        self.assertIn('--dbname=stage_one', first_args)
        self.assertIn('\\restrict abc123', first_args)
        self.assertIn('\\restrict def456', second_args)
        self.assertNotIn('\\restrict abc123', second_args)

    def test_pg_tools_use_odoo_path_and_environment(self):
        wizard = self._wizard()
        environment = {'PGHOST': 'db.internal', 'PGUSER': 'odoo'}
        with patch.object(restore_module, 'find_pg_tool', return_value='/pg/bin/psql') as find, \
                patch.object(restore_module, 'exec_pg_environ', return_value=environment), \
                patch.object(restore_module.subprocess, 'run', return_value=self._completed()) as run:
            wizard._run_pg_tool('psql', ['--dbname=postgres'])
        find.assert_called_once_with('psql')
        self.assertEqual(run.call_args.args[0], ['/pg/bin/psql', '--dbname=postgres'])
        self.assertIs(run.call_args.kwargs['env'], environment)

    def test_database_queries_use_validated_sql_literals(self):
        wizard = self._wizard()
        with patch.object(type(wizard), '_run_pg_tool', return_value=self._completed('1')) as run_tool:
            self.assertTrue(wizard._database_exists(self.target))
        self.assertEqual(
            run_tool.call_args.args[1][-1],
            "SELECT 1 FROM pg_database WHERE datname = '%s';" % self.target,
        )

        with patch.object(type(wizard), '_close_database_connections'), \
                patch.object(type(wizard), '_run_pg_tool', return_value=self._completed()) as run_tool:
            wizard._terminate_database(self.target)
        self.assertEqual(
            run_tool.call_args.args[1][-1],
            'SELECT pg_terminate_backend(pid) FROM pg_stat_activity '
            "WHERE datname = '%s' AND pid <> pg_backend_pid();" % self.target,
        )

    def test_staging_restore_and_verification_precede_target_rename(self):
        wizard = self._wizard()
        backup = self._write_zip()
        commands = []
        verification = []

        def run_tool(tool, args, **kwargs):
            commands.append((tool, list(args)))
            return self._completed()

        def verify(database):
            verification.append(database)
            commands.append(('verify', [database]))

        with patch.object(type(wizard), '_get_provider', return_value=self._provider(backup)), \
                self._filestore_patch(), \
                patch.object(type(wizard), '_database_exists', return_value=True), \
                patch.object(type(wizard), '_verify_staging_database', side_effect=verify), \
                patch.object(type(wizard), '_run_pg_tool', side_effect=run_tool):
            wizard.action_restore()

        create_index = next(i for i, (tool, _args) in enumerate(commands) if tool == 'createdb')
        restore_index = next(
            i for i, (tool, args) in enumerate(commands)
            if tool == 'psql' and '-f' in args
        )
        verify_index = next(i for i, (tool, _args) in enumerate(commands) if tool == 'verify')
        cutover_index, cutover_args = next(
            (i, args) for i, (tool, args) in enumerate(commands)
            if tool == 'psql' and any('ALTER DATABASE' in arg for arg in args)
        )
        self.assertIn('--single-transaction', cutover_args)
        cutover_sql = next(arg for arg in cutover_args if 'ALTER DATABASE' in arg)
        old_rename = 'ALTER DATABASE "%s" RENAME TO "_bv_rollback_' % self.target
        new_rename = 'RENAME TO "%s";' % self.target
        self.assertIn(old_rename, cutover_sql)
        self.assertIn(new_rename, cutover_sql)
        self.assertLess(cutover_sql.index(old_rename), cutover_sql.index(new_rename))
        self.assertLess(create_index, restore_index)
        self.assertLess(restore_index, verify_index)
        self.assertLess(verify_index, cutover_index)
        self.assertEqual(verification, [commands[verify_index][1][0]])
        # The old target may only be dropped after the new target is active.
        drop_calls = [args for tool, args in commands if tool == 'dropdb']
        self.assertTrue(drop_calls)
        self.assertTrue(all(self.target not in args for args in drop_calls))

    def test_cutover_rename_failure_keeps_target_untouched(self):
        wizard = self._wizard()
        staging = '_bv_restore_fixed'
        rollback = '_bv_rollback_fixed'
        with patch.object(type(wizard), '_scratch_database_name', return_value=rollback), \
                patch.object(type(wizard), '_rename_databases', side_effect=UserError('rename failed')) as rename, \
                patch.object(type(wizard), '_rollback_database_cutover') as rollback_names:
            with self.assertRaisesRegex(UserError, 'cutover failed'):
                wizard._database_cutover(staging, self.target, True)
        rename.assert_called_once_with([
            (self.target, rollback),
            (staging, self.target),
        ])
        # PostgreSQL's single transaction is all-or-nothing; no partial rename
        # state exists that should be "repaired" after this exception.
        rollback_names.assert_not_called()

    def test_database_rollback_restores_both_names_in_one_transaction(self):
        wizard = self._wizard()
        staging = '_bv_restore_fixed'
        rollback = '_bv_rollback_fixed'
        state = {
            'rollback_database': rollback,
            'old_moved': True,
            'new_moved': True,
        }
        with patch.object(type(wizard), '_rename_databases') as rename:
            wizard._rollback_database_cutover(state, staging, self.target)
        rename.assert_called_once_with([
            (self.target, staging),
            (rollback, self.target),
        ])
        self.assertFalse(state['old_moved'])
        self.assertFalse(state['new_moved'])

    def test_filestore_exchange_never_rmtrees_before_rename_and_rolls_back(self):
        wizard = self._wizard()
        os.makedirs(self.target_filestore)
        with open(os.path.join(self.target_filestore, 'old'), 'wb') as old:
            old.write(b'old')
        staged = os.path.join(os.path.dirname(self.target_filestore), '.staged')
        os.makedirs(staged)
        with open(os.path.join(staged, 'new'), 'wb') as new:
            new.write(b'new')
        real_rename = os.rename

        def fail_stage_rename(source, destination):
            if source == staged and destination == self.target_filestore:
                raise OSError(errno.EXDEV, 'cross-device link')
            return real_rename(source, destination)

        with patch.object(restore_module.os, 'rename', side_effect=fail_stage_rename), \
                patch.object(restore_module.shutil, 'rmtree') as rmtree:
            with self.assertRaises(OSError):
                wizard._swap_filestore(staged, self.target_filestore)
        rmtree.assert_not_called()
        self.assertTrue(os.path.isfile(os.path.join(self.target_filestore, 'old')))
        self.assertTrue(os.path.isfile(os.path.join(staged, 'new')))

    def test_filestore_is_staged_on_destination_filesystem_and_swapped(self):
        wizard = self._wizard()
        backup = self._write_zip({
            'dump.sql': b'-- dump\n',
            'manifest.json': b'{"db_name": "source_db"}',
            'filestore/aa/bb': b'new attachment',
        })
        os.makedirs(self.target_filestore)
        with open(os.path.join(self.target_filestore, 'old'), 'wb') as old:
            old.write(b'old attachment')
        observed = {}

        def run_tool(tool, args, **kwargs):
            if tool == 'psql' and any('RENAME TO "%s";' % self.target in arg for arg in args):
                filestore_root = os.path.dirname(self.target_filestore)
                staged = [
                    os.path.join(filestore_root, name)
                    for name in os.listdir(filestore_root)
                    if name.startswith('.%s.restore-' % self.target)
                ]
                self.assertEqual(len(staged), 1)
                self.assertEqual(os.path.dirname(staged[0]), filestore_root)
                self.assertTrue(os.path.isfile(os.path.join(staged[0], 'aa', 'bb')))
                self.assertTrue(os.path.isfile(os.path.join(self.target_filestore, 'old')))
                observed['staged'] = staged[0]
            return self._completed()

        with patch.object(type(wizard), '_get_provider', return_value=self._provider(backup)), \
                self._filestore_patch(), \
                patch.object(type(wizard), '_database_exists', return_value=True), \
                patch.object(type(wizard), '_verify_staging_database'), \
                patch.object(type(wizard), '_run_pg_tool', side_effect=run_tool):
            wizard.action_restore()

        self.assertTrue(os.path.isfile(os.path.join(self.target_filestore, 'aa', 'bb')))
        self.assertFalse(os.path.exists(os.path.join(self.target_filestore, 'old')))
        self.assertFalse(os.path.exists(observed['staged']))

    def test_filestore_failure_rolls_back_database_cutover(self):
        wizard = self._wizard()
        backup = self._write_zip({
            'dump.sql': b'-- dump\n',
            'manifest.json': b'{"db_name": "source_db"}',
            'filestore/aa/bb': b'new attachment',
        })
        commands = []

        def run_tool(tool, args, **kwargs):
            commands.append((tool, list(args)))
            return self._completed()

        with patch.object(type(wizard), '_get_provider', return_value=self._provider(backup)), \
                self._filestore_patch(), \
                patch.object(type(wizard), '_database_exists', return_value=True), \
                patch.object(type(wizard), '_verify_staging_database'), \
                patch.object(type(wizard), '_run_pg_tool', side_effect=run_tool), \
                patch.object(type(wizard), '_swap_filestore', side_effect=OSError('swap failed')):
            with self.assertRaisesRegex(UserError, 'rolled back'):
                wizard.action_restore()

        staging = next(args[-1] for tool, args in commands if tool == 'createdb')
        statements = [
            arg for _tool, args in commands for arg in args
            if arg.startswith('ALTER DATABASE')
        ]
        self.assertTrue(any(
            statement.startswith('ALTER DATABASE "%s" RENAME TO "_bv_rollback_' % self.target)
            for statement in statements
        ))
        self.assertTrue(any(
            'ALTER DATABASE "%s" RENAME TO "%s";' % (self.target, staging) in statement
            for statement in statements
        ))
        self.assertTrue(any(
            'ALTER DATABASE "_bv_rollback_' in statement
            and statement.endswith('RENAME TO "%s";' % self.target)
            for statement in statements
        ))

    def test_neutralization_failure_never_cuts_over_or_drops_target(self):
        wizard = self._wizard(neutralize_database=True)
        backup = self._write_zip()
        commands = []
        cursor_context = MagicMock()
        connection = SimpleNamespace(cursor=lambda: cursor_context)

        def run_tool(tool, args, **kwargs):
            commands.append((tool, list(args)))
            return self._completed()

        with patch.object(type(wizard), '_get_provider', return_value=self._provider(backup)), \
                self._filestore_patch(), \
                patch.object(type(wizard), '_database_exists', return_value=True), \
                patch.object(type(wizard), '_verify_staging_database'), \
                patch.object(type(wizard), '_run_pg_tool', side_effect=run_tool), \
                patch.object(restore_module.odoo.sql_db, 'db_connect', return_value=connection), \
                patch.object(
                    restore_module.odoo.modules.neutralize,
                    'neutralize_database',
                    side_effect=RuntimeError('neutralize failed'),
                ):
            with self.assertRaisesRegex(UserError, 'neutralization failed'):
                wizard.action_restore()

        statements = [arg for _tool, args in commands for arg in args if arg.startswith('ALTER DATABASE')]
        self.assertFalse(statements)
        drop_calls = [args for tool, args in commands if tool == 'dropdb']
        self.assertTrue(drop_calls)
        self.assertTrue(all(self.target not in args for args in drop_calls))
