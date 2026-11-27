# -*- coding: utf-8 -*-

import json
import logging
import os
import posixpath
import shlex
import shutil
import subprocess
import tempfile
import uuid
from datetime import datetime

from odoo import models, fields, api
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

try:
    import boto3
    BOTO3_AVAILABLE = True
except ImportError:
    BOTO3_AVAILABLE = False


class BackupProviderCloud(models.Model):
    """Cloud backup provider using rclone or boto3 for S3-compatible storage.

    Supports S3 / MinIO / R2 / Backblaze B2 / Google Cloud Storage / Azure Blob
    and any S3-compatible storage via rclone. rclone is the primary engine;
    boto3 is an optional alternative for S3-compatible backends only.

    rclone is detected at runtime — the provider works if either the ``rclone``
    binary is on PATH or (for S3-compatible) ``boto3`` is installed.
    """
    _name = 'backup.provider.cloud'
    _description = 'Cloud Backup Provider'
    _inherit = 'backup.provider.abstract'

    provider_type = fields.Selection(
        selection=[('cloud', 'Cloud (rclone)')],
        default='cloud',
        readonly=True,
    )

    # ── Cloud provider type ──────────────────────────────────────────────

    cloud_service = fields.Selection(
        selection=[
            ('s3', 'S3 Compatible (MinIO / R2 / DigitalOcean)'),
            ('aws_s3', 'AWS S3'),
            ('b2', 'Backblaze B2'),
            ('gcs', 'Google Cloud Storage'),
            ('azure', 'Azure Blob Storage'),
            ('webdav', 'WebDAV (Nextcloud / ownCloud / Hetzner Box)'),
        ],
        string='Cloud Service',
        default='s3',
        required=True,
        help='Target cloud storage service type',
    )

    # ── Connection settings ─────────────────────────────────────────────

    bucket = fields.Char(
        string='Bucket / Container',
        help='Bucket name (S3) or container (Azure) or remote path',
    )

    region = fields.Char(
        string='Region',
        help='AWS region (e.g. eu-central-1) or equivalent',
    )

    endpoint = fields.Char(
        string='Endpoint URL',
        help='Custom endpoint for S3-compatible services '
             '(e.g. https://s3.eu-central-1.amazonaws.com, '
             'https://fra1.digitaloceanspaces.com)',
    )

    access_key_id = fields.Char(
        string='Access Key ID',
        groups='backup_versatile.group_backup_admin',
        help='Access key / application key for the cloud service',
    )

    secret_access_key = fields.Char(
        string='Secret Access Key',
        groups='backup_versatile.group_backup_admin',
        help='Secret key / application secret for the cloud service',
    )

    acl = fields.Selection(
        selection=[
            ('private', 'Private'),
            ('bucket-owner-full-control', 'Bucket Owner Full Control'),
        ],
        string='Upload ACL',
        default='private',
        help='Access control for uploaded objects',
    )

    storage_class = fields.Selection(
        selection=[
            ('STANDARD', 'Standard'),
            ('GLACIER', 'Glacier (archive)'),
            ('DEEP_ARCHIVE', 'Deep Archive'),
        ],
        string='Storage Class',
        default='STANDARD',
        help='S3 storage class for uploaded objects',
    )

    # ── Pre-configured remote (advanced) ────────────────────────────────

    use_custom_remote = fields.Boolean(
        string='Use Pre-configured rclone Remote',
        default=False,
        help='Use a remote already configured via ``rclone config`` on the '
             'server instead of the connection fields above. '
             'Enable this if you have a complex rclone setup.',
    )
    custom_remote_name = fields.Char(
        string='rclone Remote Name',
        help='Name of the pre-configured rclone remote '
             '(e.g. ``my-s3-remote``)',
    )
    custom_remote_path = fields.Char(
        string='Remote Path',
        help='Path inside the remote (e.g. ``backups/odoo``)',
    )

    # ── rclone flags ────────────────────────────────────────────────────

    rclone_flags = fields.Char(
        string='Extra rclone Flags',
        help='Additional rclone flags passed to every command '
             '(e.g. ``--checksum --verbose``)',
    )

    # ── Internal helpers ────────────────────────────────────────────────

    def _rclone_available(self):
        """Return True if the ``rclone`` binary is on PATH."""
        return bool(shutil.which('rclone'))

    @staticmethod
    def _normalize_dt(value):
        """Return a naive UTC datetime so mixed tz-aware/naive values can be
        sorted together (rclone returns tz-aware, boto3 may return naive)."""
        if value and getattr(value, 'tzinfo', None) is not None:
            return value.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        return value

    def _boto3_available(self):
        """Return True if boto3 is installed and usable for direct S3 mode."""
        return (
            BOTO3_AVAILABLE
            and not self.use_custom_remote
            and self.cloud_service in ('s3', 'aws_s3')
        )

    def _build_rclone_command(self, operation, src, dst, extras=None):
        """Build an ``rclone`` subprocess command list.

        Args:
            operation: rclone subcommand (``copy``, ``ls``, ``delete``, …)
            src: source path / remote ref
            dst: destination path / remote ref
            extras: optional list of additional flags

        Returns:
            list[str]: command list ready for ``subprocess.run()``
        """
        cmd = ['rclone', operation]

        # Common flags
        cmd.extend(['--progress', '--stats', '5s'])

        if self.use_custom_remote and self.custom_remote_name:
            # Use pre-configured remote – no extra auth flags
            pass
        else:
            # Build auth flags from the connection fields
            service = self.cloud_service
            if service == 's3' or service == 'aws_s3':
                cmd.extend(['--s3-provider', 'AWS' if service == 'aws_s3' else 'Other'])
                if self.endpoint:
                    cmd.extend(['--s3-endpoint', self.endpoint])
                if self.region:
                    cmd.extend(['--s3-region', self.region])
                if self.acl:
                    cmd.extend(['--s3-acl', self.acl])
                if self.storage_class:
                    cmd.extend(['--s3-storage-class', self.storage_class])
            elif service == 'webdav':
                cmd.extend(['--webdav-url', self.endpoint or self.bucket])
            # Credentials (access_key_id / secret_access_key) for every backend
            # are injected via environment variables by _run_rclone (see
            # _rclone_secret_env) so they never leak through /proc/<pid>/cmdline.

        # Extra user-supplied flags. shlex preserves quoted values while the
        # subprocess still receives an argument list (never shell=True).
        if self.rclone_flags:
            cmd.extend(shlex.split(self.rclone_flags))

        if extras:
            cmd.extend(extras)

        # Append src and dst. Do NOT append empty strings: rclone commands
        # like ``ls``/``lsjson``/``deletefile`` take a single remote argument
        # and an extra empty positional makes them fail.
        if src:
            cmd.append(src)
        if dst:
            cmd.append(dst)

        return cmd

    def _rclone_secret_env(self):
        """Return cloud credentials as rclone environment variables.

        Secrets are passed through the environment rather than CLI flags so
        they are not exposed in the process table (``/proc/<pid>/cmdline`` is
        world-readable on Linux). rclone reads backend options from
        ``RCLONE_<BACKEND>_<OPTION>`` env vars.
        """
        env = {}
        if self.use_custom_remote and self.custom_remote_name:
            return env  # auth lives in the pre-configured remote (rclone.conf)
        service = self.cloud_service
        user = self.access_key_id or ''
        secret = self.secret_access_key or ''
        if service in ('s3', 'aws_s3'):
            if user:
                env['RCLONE_S3_ACCESS_KEY_ID'] = user
            if secret:
                env['RCLONE_S3_SECRET_ACCESS_KEY'] = secret
        elif service == 'b2':
            if user:
                env['RCLONE_B2_ACCOUNT'] = user
            if secret:
                env['RCLONE_B2_KEY'] = secret
        elif service == 'gcs':
            if user:
                env['RCLONE_GCS_CLIENT_ID'] = user
            if secret:
                env['RCLONE_GCS_CLIENT_SECRET'] = secret
        elif service == 'azure':
            if user:
                env['RCLONE_AZUREBLOB_ACCOUNT'] = user
            if secret:
                env['RCLONE_AZUREBLOB_KEY'] = secret
        elif service == 'webdav':
            if user:
                env['RCLONE_WEBDAV_USER'] = user
            if secret:
                env['RCLONE_WEBDAV_PASS'] = secret
        return env

    def _run_rclone(self, cmd):
        """Execute an rclone command and return parsed output.

        Returns:
            dict with ``success``, ``message``, ``stdout``, ``stderr``
        """
        try:
            env = os.environ.copy()
            env.update(self._rclone_secret_env())
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=3600,
                env=env,
            )
            output = (result.stdout or '') + (result.stderr or '')
            if result.returncode == 0:
                return {'success': True, 'message': output.strip(), 'stdout': result.stdout}
            else:
                error_msg = result.stderr.strip() or f'rclone exited with code {result.returncode}'
                return {'success': False, 'message': error_msg, 'stdout': result.stdout}
        except FileNotFoundError:
            return {'success': False, 'message': 'rclone binary not found on PATH. Install rclone (https://rclone.org/install/).'}
        except subprocess.TimeoutExpired:
            return {'success': False, 'message': 'rclone command timed out after 3600s'}
        except Exception as e:
            return {'success': False, 'message': f'rclone error: {e}'}

    def _remote_file_size(self, remote_path, object_key=None):
        """Return the remote object size, or raise when it cannot be read."""
        if not self._rclone_available():
            if not self._boto3_available():
                raise UserError('rclone or boto3 is required for cloud storage')
            import boto3 as b3
            client = b3.Session(
                aws_access_key_id=self.access_key_id,
                aws_secret_access_key=self.secret_access_key,
                region_name=self.region,
            ).client('s3', endpoint_url=self.endpoint)
            key = object_key or self._sanitize_remote_key(remote_path.rsplit('/', 1)[-1])
            return client.head_object(Bucket=self.bucket, Key=key)['ContentLength']

        result = self._run_rclone(self._build_rclone_command(
            'lsjson', remote_path, '', extras=['--stat'],
        ))
        if not result['success']:
            raise UserError('Could not verify uploaded object: %s' % result['message'])
        try:
            metadata = json.loads(result['stdout'] or '[]')
            if isinstance(metadata, list):
                metadata = metadata[0]
            return int(metadata['Size'])
        except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as e:
            raise UserError('Could not verify uploaded object: %s' % e) from e

    def _preflight_check(self):
        """Prove cloud write/delete access before an expensive database dump."""
        self.ensure_one()
        probe_name = '.backup_versatile_probe_%s' % uuid.uuid4().hex
        probe_path = self._remote_path(probe_name)
        probe_created = False
        try:
            if not self._rclone_available():
                if not self._boto3_available():
                    return {'success': False, 'message': 'rclone or boto3 is required for cloud storage'}
                import boto3 as b3
                client = b3.Session(
                    aws_access_key_id=self.access_key_id,
                    aws_secret_access_key=self.secret_access_key,
                    region_name=self.region,
                ).client('s3', endpoint_url=self.endpoint)
                client.put_object(Bucket=self.bucket, Key=probe_name, Body=b'probe')
                probe_created = True
                client.delete_object(Bucket=self.bucket, Key=probe_name)
                probe_created = False
                return {'success': True, 'message': 'Cloud destination is writable'}

            with tempfile.NamedTemporaryFile(prefix='backup_cloud_probe_') as probe:
                probe.write(b'probe')
                probe.flush()
                result = self._run_rclone(self._build_rclone_command(
                    'copyto', probe.name, probe_path,
                ))
                if not result['success']:
                    return {'success': False, 'message': result['message']}
                probe_created = True
                if self._remote_file_size(probe_path, probe_name) != 5:
                    return {'success': False, 'message': 'Cloud probe verification failed'}
                result = self._run_rclone(self._build_rclone_command(
                    'deletefile', probe_path, '',
                ))
                if not result['success']:
                    return {'success': False, 'message': result['message']}
                probe_created = False
            return {'success': True, 'message': 'Cloud destination is writable'}
        except Exception as e:
            return {'success': False, 'message': 'Cloud preflight failed: %s' % e}
        finally:
            if probe_created:
                try:
                    if self._rclone_available():
                        self._run_rclone(self._build_rclone_command(
                            'deletefile', probe_path, '',
                        ))
                    else:
                        import boto3 as b3
                        client = b3.Session(
                            aws_access_key_id=self.access_key_id,
                            aws_secret_access_key=self.secret_access_key,
                            region_name=self.region,
                        ).client('s3', endpoint_url=self.endpoint)
                        client.delete_object(Bucket=self.bucket, Key=probe_name)
                except Exception as cleanup_error:
                    _logger.warning(
                        'Could not remove cloud preflight probe %s: %s',
                        probe_path, cleanup_error,
                    )

    # ── Remote path helpers ─────────────────────────────────────────────

    def _remote_base(self):
        """Return the base remote path for this provider."""
        if self.use_custom_remote and self.custom_remote_name:
            prefix = '%s:%s' % (self.custom_remote_name, self.custom_remote_path or '')
        else:
            # A leading colon tells rclone to create an on-the-fly backend.
            # Without it, ``s3:bucket`` means a configured remote named ``s3``.
            prefix = ':%s:%s' % (self._rclone_remote_name(), self.bucket or '')
        return prefix.rstrip('/')

    def _rclone_remote_name(self):
        """Return an ephemeral remote name for this provider type."""
        # rclone can work with `:s3` or `:b2` type prefixes inline
        # when passing auth flags – we use the bucket as the remote ref
        mapping = {
            's3': 's3',
            'aws_s3': 's3',
            'b2': 'b2',
            'gcs': 'gcs',
            'azure': 'azureblob',
            'webdav': 'webdav',
        }
        return mapping.get(self.cloud_service, 's3')

    @staticmethod
    def _sanitize_remote_key(filename):
        """Return a safe relative object key without flattening nested keys."""
        if not filename:
            return filename
        if '\x00' in filename:
            raise UserError("Invalid filename: null byte")
        normalized = posixpath.normpath(filename.replace('\\', '/'))
        if normalized in ('', '.', '..') or normalized.startswith('/'):
            raise UserError("Invalid filename: %r" % filename)
        if any(part == '..' for part in normalized.split('/')):
            raise UserError("Invalid filename: %r" % filename)
        return normalized

    def _remote_path(self, filename=''):
        """Full remote path for a given filename or relative object key."""
        base = self._remote_base()
        if filename:
            safe = self._sanitize_remote_key(filename)
            return '%s/%s' % (base, safe)
        return base

    # ── Implement abstract interface ────────────────────────────────────

    @api.private
    def test_connection(self):
        """Test cloud storage connection by listing the bucket."""
        self.ensure_one()

        # Check rclone availability first
        if not self._rclone_available():
            if self.cloud_service in ('s3', 'aws_s3') and self._boto3_available():
                return self._test_connection_boto3()
            return {
                'success': False,
                'message': (
                    'rclone is not installed. Install it from https://rclone.org/install/\n'
                    'or ``sudo apt install rclone`` (Debian/Ubuntu).\n'
                    'For S3-only usage, also install boto3: ``pip install boto3``'
                ),
            }

        cmd = self._build_rclone_command('ls', self._remote_path(), '', extras=['--max-depth', '1'])
        result = self._run_rclone(cmd)

        if result['success']:
            return {
                'success': True,
                'message': 'Cloud storage accessible: %s\n%s' % (self._remote_path(), result['message']),
            }
        return {
            'success': False,
            'message': 'Connection failed: %s' % result['message'],
        }

    def _test_connection_boto3(self):
        """Fallback connection test using boto3 (S3-only)."""
        try:
            import boto3 as b3
            session = b3.Session(
                aws_access_key_id=self.access_key_id,
                aws_secret_access_key=self.secret_access_key,
                region_name=self.region,
            )
            client = session.client('s3', endpoint_url=self.endpoint)
            client.head_bucket(Bucket=self.bucket)
            return {
                'success': True,
                'message': 'S3 bucket accessible: %s (via boto3)' % self.bucket,
            }
        except Exception as e:
            return {
                'success': False,
                'message': 'S3 connection failed (boto3): %s' % e,
            }

    @api.private
    def upload_backup(self, backup_file_path, remote_filename):
        """Upload backup file to cloud storage."""
        self.ensure_one()

        if not self._rclone_available():
            if self.cloud_service in ('s3', 'aws_s3') and self._boto3_available():
                return self._upload_backup_boto3(backup_file_path, remote_filename)
            return {
                'success': False,
                'message': 'rclone not installed. Install from https://rclone.org/install/',
                'metadata': {},
            }

        remote_path = self._remote_path(remote_filename)
        cmd = self._build_rclone_command(
            'copyto', backup_file_path, remote_path, extras=['--checksum'],
        )
        result = self._run_rclone(cmd)

        if result['success']:
            # Get file size and prove the final object exists with that size.
            try:
                size_bytes = os.path.getsize(backup_file_path)
            except OSError:
                size_bytes = 0
            try:
                remote_size = self._remote_file_size(
                    remote_path, self._sanitize_remote_key(remote_filename),
                )
            except Exception as e:
                return {
                    'success': False,
                    'message': 'Upload verification failed: %s' % e,
                    'metadata': {},
                }
            if remote_size != size_bytes:
                return {
                    'success': False,
                    'message': 'Upload verification failed: local %d, remote %d' % (size_bytes, remote_size),
                    'metadata': {},
                }
            return {
                'success': True,
                'message': 'Uploaded to %s' % remote_path,
                'metadata': {
                    'remote_path': remote_path,
                    'size_bytes': size_bytes,
                    'size_gb': size_bytes / (1024**3),
                    'upload_date': datetime.now(),
                },
            }
        return {
            'success': False,
            'message': 'Upload failed: %s' % result['message'],
            'metadata': {},
        }

    def _upload_backup_boto3(self, backup_file_path, remote_filename):
        """Fallback S3 upload using boto3."""
        try:
            import boto3 as b3
            object_key = self._sanitize_remote_key(remote_filename)
            session = b3.Session(
                aws_access_key_id=self.access_key_id,
                aws_secret_access_key=self.secret_access_key,
                region_name=self.region,
            )
            client = session.client('s3', endpoint_url=self.endpoint)
            extra_args = {}
            if self.acl:
                extra_args['ACL'] = self.acl
            if self.storage_class:
                extra_args['StorageClass'] = self.storage_class
            client.upload_file(
                backup_file_path, self.bucket, object_key,
                ExtraArgs=extra_args,
            )
            size_bytes = os.path.getsize(backup_file_path)
            remote_size = client.head_object(
                Bucket=self.bucket, Key=object_key,
            )['ContentLength']
            if remote_size != size_bytes:
                raise UserError(
                    'Upload verification failed: local %d, remote %d'
                    % (size_bytes, remote_size)
                )
            return {
                'success': True,
                'message': 'Uploaded to s3://%s/%s' % (self.bucket, remote_filename),
                'metadata': {
                    'remote_path': 's3://%s/%s' % (self.bucket, remote_filename),
                    'size_bytes': size_bytes,
                    'size_gb': size_bytes / (1024**3),
                    'upload_date': datetime.now(),
                },
            }
        except Exception as e:
            return {
                'success': False,
                'message': 'S3 upload failed: %s' % e,
                'metadata': {},
            }

    @api.private
    def download_backup(self, remote_filename, local_path):
        """Download backup file from cloud storage."""
        self.ensure_one()

        if not self._rclone_available():
            if self.cloud_service in ('s3', 'aws_s3') and self._boto3_available():
                return self._download_backup_boto3(remote_filename, local_path)
            return {
                'success': False,
                'message': 'rclone not installed.',
            }

        remote_path = self._remote_path(remote_filename)
        cmd = self._build_rclone_command('copyto', remote_path, local_path)
        result = self._run_rclone(cmd)

        if result['success'] and os.path.exists(local_path):
            return {
                'success': True,
                'message': 'Downloaded to %s' % local_path,
            }
        return {
            'success': False,
            'message': 'Download failed: %s' % result['message'],
        }

    def _download_backup_boto3(self, remote_filename, local_path):
        """Fallback S3 download using boto3."""
        try:
            import boto3 as b3
            object_key = self._sanitize_remote_key(remote_filename)
            session = b3.Session(
                aws_access_key_id=self.access_key_id,
                aws_secret_access_key=self.secret_access_key,
                region_name=self.region,
            )
            client = session.client('s3', endpoint_url=self.endpoint)
            client.download_file(self.bucket, object_key, local_path)
            return {
                'success': True,
                'message': 'Downloaded to %s' % local_path,
            }
        except Exception as e:
            return {
                'success': False,
                'message': 'S3 download failed: %s' % e,
            }

    @api.private
    def list_backups(self, prefix=None):
        """List backup files in cloud storage."""
        self.ensure_one()

        if not self._rclone_available():
            if self.cloud_service in ('s3', 'aws_s3') and self._boto3_available():
                return self._list_backups_boto3(prefix)
            return []

        cmd = self._build_rclone_command(
            'lsjson', self._remote_path(), '',
            extras=['--recursive', '--max-depth', '3'],
        )
        result = self._run_rclone(cmd)

        if not result['success']:
            raise UserError('Failed to list cloud backups: %s' % result['message'])

        try:
            raw = json.loads(result['stdout'] or '[]')
        except (json.JSONDecodeError, ValueError) as e:
            raise UserError('Invalid rclone lsjson output: %s' % e) from e

        backups = []
        for entry in raw:
            name = entry.get('Name', '')
            if prefix and not name.startswith(prefix):
                continue
            if not self._is_backup_file(name):
                continue
            backups.append({
                'filename': name,
                'remote_path': self._remote_path(name),
                'size_bytes': entry.get('Size', 0),
                'size_gb': (entry.get('Size', 0) or 0) / (1024**3),
                'modified_date': datetime.fromisoformat(
                    entry['ModTime'].replace('Z', '+00:00')
                ) if entry.get('ModTime') else None,
            })

        backups.sort(
            key=lambda x: self._normalize_dt(x['modified_date']) or datetime.min,
            reverse=True,
        )
        return backups

    def _list_backups_boto3(self, prefix=None):
        """Fallback S3 listing using boto3."""
        try:
            import boto3 as b3
            session = b3.Session(
                aws_access_key_id=self.access_key_id,
                aws_secret_access_key=self.secret_access_key,
                region_name=self.region,
            )
            client = session.client('s3', endpoint_url=self.endpoint)
            kwargs = {'Bucket': self.bucket}
            if prefix:
                kwargs['Prefix'] = prefix
            paginator = client.get_paginator('list_objects_v2')
            contents = [
                obj
                for page in paginator.paginate(**kwargs)
                for obj in page.get('Contents', [])
            ]
            backups = []
            for obj in contents:
                name = obj['Key']
                if not self._is_backup_file(name):
                    continue
                backups.append({
                    'filename': name,
                    'remote_path': 's3://%s/%s' % (self.bucket, name),
                    'size_bytes': obj['Size'],
                    'size_gb': obj['Size'] / (1024**3),
                    'modified_date': obj['LastModified'],
                })
            backups.sort(
                key=lambda x: self._normalize_dt(x['modified_date']) or datetime.min,
                reverse=True,
            )
            return backups
        except Exception as e:
            raise UserError('Failed to list S3 backups: %s' % e) from e

    @api.private
    def delete_backup(self, remote_filename):
        """Delete backup file from cloud storage."""
        self.ensure_one()

        if not self._rclone_available():
            if self.cloud_service in ('s3', 'aws_s3') and self._boto3_available():
                return self._delete_backup_boto3(remote_filename)
            return {'success': False, 'message': 'rclone not installed.'}

        remote_path = self._remote_path(remote_filename)
        cmd = self._build_rclone_command('deletefile', remote_path, '')
        result = self._run_rclone(cmd)

        if result['success']:
            return {'success': True, 'message': 'Deleted: %s' % remote_path}
        return {'success': False, 'message': result['message']}

    def _delete_backup_boto3(self, remote_filename):
        """Fallback S3 delete using boto3."""
        try:
            import boto3 as b3
            object_key = self._sanitize_remote_key(remote_filename)
            session = b3.Session(
                aws_access_key_id=self.access_key_id,
                aws_secret_access_key=self.secret_access_key,
                region_name=self.region,
            )
            client = session.client('s3', endpoint_url=self.endpoint)
            client.delete_object(Bucket=self.bucket, Key=object_key)
            return {'success': True, 'message': 'Deleted s3://%s/%s' % (self.bucket, remote_filename)}
        except Exception as e:
            return {'success': False, 'message': 'Delete failed: %s' % e}

    @api.private
    def get_storage_info(self):
        """Get cloud storage usage information."""
        self.ensure_one()

        backups = self.list_backups()
        total_size = sum(b['size_bytes'] for b in backups)

        return {
            'provider_type': 'cloud',
            'backup_count': len(backups),
            'total_backup_size_gb': total_size / (1024**3),
            'storage_path': self._remote_path(),
        }

    def _is_backup_file(self, filename):
        """Check if filename is a backup file."""
        backup_extensions = ['.zip', '.dump', '.sql', '.tar.gz', '.tgz']
        return any(filename.lower().endswith(ext) for ext in backup_extensions)

    # ── Validation ──────────────────────────────────────────────────────

    @api.constrains('bucket', 'use_custom_remote', 'custom_remote_name')
    def _check_bucket(self):
        for record in self:
            if record.use_custom_remote:
                if not record.custom_remote_name:
                    raise UserError('Custom rclone remote name is required.')
            elif not record.bucket:
                raise UserError('Bucket / Container is required.')

    def action_reset_custom_remote(self):
        """Reset the custom remote configuration."""
        self._assert_backup_admin()
        self.ensure_one()
        self.write({
            'use_custom_remote': False,
            'custom_remote_name': False,
            'custom_remote_path': False,
        })
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Custom remote reset',
                'message': 'Using connection fields instead of pre-configured remote.',
                'type': 'info',
                'sticky': False,
            },
        }
