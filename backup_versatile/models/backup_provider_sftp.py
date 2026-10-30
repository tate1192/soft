# -*- coding: utf-8 -*-

import base64
import contextlib
import datetime
import errno
import hashlib
import logging
import os
import shlex
import tempfile
import types

from odoo import models, fields, api
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

try:
    import paramiko
except ImportError:
    paramiko = None
    _logger.warning(
        "Paramiko library not found. Please install it with: pip install paramiko"
    )


if paramiko:
    class _CaptureHostKeyPolicy(paramiko.MissingHostKeyPolicy):
        """Trust-on-first-use policy: accept the key once and remember it.

        Used only while no host key has been pinned yet. The captured key is
        pinned on the provider record right after a successful connection, so
        every later connection is verified against it.
        """

        def __init__(self):
            self.key = None

        def missing_host_key(self, client, hostname, key):
            self.key = key
else:
    _CaptureHostKeyPolicy = None


class BackupProviderSftp(models.Model):
    """
    SFTP backup provider built on Paramiko.

    Paramiko is used instead of AsyncSSH: it is the de-facto standard SSH
    library for Python, it is already present in Odoo server environments, and
    it does not pin transitive dependencies (cryptography) in a way that
    conflicts with Odoo's own pins.

    The public contract is unchanged: ``test_connection``, ``upload_backup``,
    ``download_backup``, ``list_backups``, ``delete_backup`` and
    ``get_storage_info`` keep the same signatures and return values as before.
    """
    _name = 'backup.provider.sftp'
    _description = 'SFTP Backup Provider'
    _inherit = 'backup.provider.abstract'

    provider_type = fields.Selection(
        selection=[('sftp', 'SFTP/SSH')],
        default='sftp',
        readonly=True
    )

    # SFTP-specific configuration
    hostname = fields.Char(
        string='Hostname/IP',
        required=True,
        help='SFTP server hostname or IP address'
    )
    port = fields.Integer(
        string='Port',
        default=22,
        required=True,
        help='SFTP server port (default: 22)'
    )
    username = fields.Char(
        string='Username',
        required=True,
        help='Username for SFTP authentication'
    )
    auth_type = fields.Selection(
        selection=[('password', 'Password'), ('key', 'SSH Private Key')],
        string='Authentication Type',
        default='password',
        required=True,
        help='Authentication method for SFTP connection'
    )
    password = fields.Char(
        string='Password',
        groups='backup_versatile.group_backup_admin',
        help='Password for password-based authentication'
    )
    private_key = fields.Text(
        string='SSH Private Key',
        groups='backup_versatile.group_backup_admin',
        help='Paste your private key content (RSA, ECDSA, Ed25519). '
             'Leave empty to use ~/.ssh/id_* default keys.',
    )
    private_key_passphrase = fields.Char(
        string='Key Passphrase',
        groups='backup_versatile.group_backup_admin',
        help='Passphrase for the private key (if encrypted)',
    )
    remote_directory = fields.Char(
        string='Remote Directory',
        default='/home/backups',
        required=True,
        help='Remote directory where backups will be stored'
    )
    create_remote_dirs = fields.Boolean(
        string='Create Remote Directories',
        default=True,
        help='Automatically create remote directories if they do not exist'
    )
    connection_timeout = fields.Integer(
        string='Connection Timeout (seconds)',
        default=30,
        help='Timeout for establishing SSH connection'
    )
    transfer_timeout = fields.Integer(
        string='Transfer Timeout (seconds)',
        default=3600,
        help='Timeout for file transfer operations (0 = no timeout)'
    )
    preserve_file_permissions = fields.Boolean(
        string='Preserve File Permissions',
        default=True,
        help='Preserve file permissions during transfer'
    )
    max_concurrent_transfers = fields.Integer(
        string='Max Concurrent Transfers',
        default=1,
        help='Kept for backward compatibility. Transfers are performed '
             'sequentially by Paramiko, so this value has no effect.'
    )

    # Host key security (trust on first use)
    verify_host_key = fields.Boolean(
        string='Verify Server Host Key',
        default=True,
        help='Pin the server host key on the first successful connection and '
             'verify it on every connection after that (trust on first use). '
             'Disable only for testing: without verification, connections are '
             'vulnerable to man-in-the-middle attacks.'
    )
    server_host_key = fields.Text(
        string='Pinned Server Host Key',
        readonly=True,
        copy=False,
        help='Public host key of the SFTP server, captured automatically on '
             'the first successful connection. Later connections must present '
             'the same key. Use "Reset Pinned Host Key" if the server was '
             'legitimately reinstalled or migrated.'
    )
    host_key_fingerprint = fields.Char(
        string='Host Key Fingerprint',
        readonly=True,
        copy=False,
        help='SHA-256 fingerprint of the pinned server host key. Compare it '
             'with the output of "ssh-keyscan <hostname>" on a trusted '
             'machine to confirm you are talking to the right server.'
    )

    # Override abstract methods
    @api.private
    def test_connection(self):
        """Test SFTP connection and permissions."""
        self.ensure_one()

        if not paramiko:
            error_msg = "Paramiko library not available. Please install it with: pip install paramiko"
            return {
                'success': False,
                'message': error_msg
            }

        try:
            return self._test_connection_impl()
        except Exception as e:
            error_msg = "Connection test failed: %s" % self._describe_connection_error(e)

            return {
                'success': False,
                'message': error_msg
            }

    def _test_connection_impl(self):
        """Check directory access, write permission and host key status."""
        with self._sftp_session() as session:
            sftp = session.sftp

            # Test remote directory access
            try:
                sftp.listdir(self.remote_directory)
                directory_status = "Remote directory accessible: %s" % self.remote_directory
            except (IOError, OSError) as e:
                if not self._is_missing_path_error(e):
                    raise
                if self.create_remote_dirs:
                    self._ensure_remote_directory(sftp, self.remote_directory)
                    directory_status = "Remote directory created: %s" % self.remote_directory
                else:
                    raise UserError("Remote directory does not exist: %s" % self.remote_directory)

            # Test write permissions by creating a test file
            test_file = "%s/.backup_test_%s" % (
                self.remote_directory.rstrip('/'),
                datetime.datetime.now().strftime('%Y%m%d_%H%M%S'),
            )

            try:
                with sftp.open(test_file, 'w') as f:
                    f.write('test')

                # Clean up test file
                sftp.remove(test_file)

                write_status = "Write permissions: OK"
            except Exception as e:
                raise UserError("No write permission in remote directory: %s" % str(e))

            # Get server information
            server_info = self._get_server_version(session.client)

            if session.pinned_fingerprint:
                host_key_status = (
                    "Host key pinned: %s\n"
                    "Verify it matches your server: ssh-keyscan %s"
                    % (session.pinned_fingerprint, self.hostname)
                )
            elif self.verify_host_key and self.server_host_key:
                host_key_status = "Host key verified: %s" % self.host_key_fingerprint
            else:
                host_key_status = "Host key verification: DISABLED (not recommended)"

            message = (
                f"{directory_status}\n"
                f"{write_status}\n"
                f"Server: {server_info}\n"
                f"{host_key_status}\n"
                f"Connection: Successful"
            )

            return {
                'success': True,
                'message': message
            }

    @api.private
    def upload_backup(self, backup_file_path, remote_filename):
        """Upload backup file via SFTP."""
        self.ensure_one()

        if not paramiko:
            return {
                'success': False,
                'message': "Paramiko library not available"
            }

        try:
            return self._upload_backup_impl(backup_file_path, remote_filename)
        except Exception as e:
            return {
                'success': False,
                'message': "Upload failed: %s" % self._describe_connection_error(e),
                'metadata': {}
            }

    @staticmethod
    def _sanitize_remote_filename(filename):
        """Return a safe basename, rejecting path traversal attempts.

        ``remote_filename`` may originate from free-text input (restore
        wizard); strip any directory components and reject ``..``/null bytes.
        """
        if not filename:
            return filename
        if '\x00' in filename:
            raise UserError("Invalid filename: null byte not allowed")
        safe = os.path.basename(filename.rstrip('/'))
        if safe in ('', '.', '..'):
            raise UserError(
                "Invalid filename: path components are not allowed (%r)" % filename
            )
        return safe

    def _upload_backup_impl(self, backup_file_path, remote_filename):
        """Upload backup file and verify the transferred size."""
        with self._sftp_session() as session:
            sftp = session.sftp

            # Ensure remote directory exists
            if self.create_remote_dirs:
                self._ensure_remote_directory(sftp, self.remote_directory)

            # Upload file
            remote_path = "%s/%s" % (
                self.remote_directory.rstrip('/'),
                self._sanitize_remote_filename(remote_filename),
            )

            # ``confirm=True`` makes Paramiko verify the remote size itself
            sftp.put(backup_file_path, remote_path, confirm=True)

            if self.preserve_file_permissions:
                sftp.chmod(remote_path, os.stat(backup_file_path).st_mode)

            # Verify upload by checking file size
            local_stats = os.stat(backup_file_path)
            remote_stats = sftp.stat(remote_path)

            if local_stats.st_size != remote_stats.st_size:
                raise UserError(
                    "Upload verification failed. Local size: %d, Remote size: %d"
                    % (local_stats.st_size, remote_stats.st_size)
                )

            return {
                'success': True,
                'message': "Backup uploaded successfully to: %s" % remote_path,
                'metadata': {
                    'remote_path': remote_path,
                    'size_bytes': remote_stats.st_size,
                    'size_gb': remote_stats.st_size / (1024**3),
                    'upload_date': datetime.datetime.now(),
                    'permissions': oct(remote_stats.st_mode)
                }
            }

    def _get_destination_path(self):
        """Return the remote destination (used in job logs)."""
        return "%s:%s" % (self.hostname or '?', self.remote_directory or '?')

    def _preflight_check(self):
        """Open the connection and make sure the remote directory is usable.

        Runs before the database dump: producing a multi-gigabyte dump that
        can only fail because the host is unreachable or the remote directory
        cannot be created is wasted work. Creating the directory here is the
        same side effect the upload would have.
        """
        self.ensure_one()

        if not paramiko:
            return {
                'success': False,
                'message': ("Paramiko library not available. Please install it "
                            "with: pip install paramiko"),
            }

        if not self.remote_directory:
            return {
                'success': False,
                'message': ("Remote Directory is not set. Fill it in the provider "
                            "form (Storage Settings > Directory Configuration)."),
            }

        try:
            with self._sftp_session() as session:
                self._ensure_remote_directory(session.sftp, self.remote_directory)
        except Exception as e:
            return {'success': False, 'message': self._describe_connection_error(e)}

        return {
            'success': True,
            'message': 'Remote destination is ready: %s' % self.remote_directory,
        }

    @api.private
    def download_backup(self, remote_filename, local_path):
        """Download backup file via SFTP."""
        self.ensure_one()

        if not paramiko:
            return {
                'success': False,
                'message': "Paramiko library not available"
            }

        try:
            return self._download_backup_impl(remote_filename, local_path)
        except Exception as e:
            return {
                'success': False,
                'message': "Download failed: %s" % self._describe_connection_error(e)
            }

    def _download_backup_impl(self, remote_filename, local_path):
        """Download backup file and verify the transferred size."""
        with self._sftp_session() as session:
            sftp = session.sftp
            remote_path = "%s/%s" % (
                self.remote_directory.rstrip('/'),
                self._sanitize_remote_filename(remote_filename),
            )

            # Check if remote file exists
            try:
                remote_stats = sftp.stat(remote_path)
            except (IOError, OSError) as e:
                if self._is_missing_path_error(e):
                    raise UserError("Remote backup file not found: %s" % remote_path)
                raise

            # Download file
            sftp.get(remote_path, local_path)

            # Verify download
            local_stats = os.stat(local_path)
            if local_stats.st_size != remote_stats.st_size:
                raise UserError(
                    "Download verification failed. Expected size: %d, Actual size: %d"
                    % (remote_stats.st_size, local_stats.st_size)
                )

            return {
                'success': True,
                'message': "Backup downloaded successfully to: %s" % local_path
            }

    @api.private
    def list_backups(self, prefix=None):
        """List backup files on SFTP server."""
        self.ensure_one()

        if not paramiko:
            return []

        try:
            return self._list_backups_impl(prefix)
        except Exception as e:
            _logger.error("Failed to list backups: %s", str(e))
            return []

    def _list_backups_impl(self, prefix=None):
        """List backup files using an existing/reused SFTP session."""
        with self._sftp_session() as session:
            return self._collect_backups(session.sftp, prefix)

    def _collect_backups(self, sftp, prefix=None):
        """Build the backup file list for an open SFTP session."""
        try:
            filenames = sftp.listdir(self.remote_directory)
        except (IOError, OSError) as e:
            if self._is_missing_path_error(e):
                return []
            raise

        backups = []

        for filename in filenames:
            # Filter by prefix if specified
            if prefix and not filename.startswith(prefix):
                continue

            # Check if it's a backup file
            if self._is_backup_file(filename):
                try:
                    # Get file attributes separately
                    file_path = "%s/%s" % (
                        self.remote_directory.rstrip('/'),
                        self._sanitize_remote_filename(filename),
                    )
                    file_attr = sftp.stat(file_path)

                    backups.append({
                        'filename': filename,
                        'remote_path': file_path,
                        'size_bytes': file_attr.st_size,
                        'size_gb': file_attr.st_size / (1024**3),
                        'modified_date': datetime.datetime.fromtimestamp(file_attr.st_mtime),
                        'permissions': oct(file_attr.st_mode) if file_attr.st_mode else None
                    })
                except Exception as e:
                    _logger.warning("Failed to get attributes for file %s: %s", filename, str(e))

        # Sort by modification date (newest first)
        backups.sort(key=lambda x: x['modified_date'], reverse=True)

        return backups

    @api.private
    def delete_backup(self, remote_filename):
        """Delete backup file from SFTP server."""
        self.ensure_one()

        if not paramiko:
            return {
                'success': False,
                'message': "Paramiko library not available"
            }

        try:
            return self._delete_backup_impl(remote_filename)
        except Exception as e:
            return {
                'success': False,
                'message': "Delete failed: %s" % self._describe_connection_error(e)
            }

    def _delete_backup_impl(self, remote_filename):
        """Delete a remote backup file."""
        with self._sftp_session() as session:
            sftp = session.sftp
            remote_path = "%s/%s" % (
                self.remote_directory.rstrip('/'),
                self._sanitize_remote_filename(remote_filename),
            )

            try:
                sftp.remove(remote_path)
                return {
                    'success': True,
                    'message': "Backup file deleted successfully: %s" % remote_path
                }
            except (IOError, OSError) as e:
                if self._is_missing_path_error(e):
                    return {
                        'success': False,
                        'message': "Backup file not found: %s" % remote_path
                    }
                raise

    @api.private
    def get_storage_info(self):
        """Get SFTP storage information."""
        self.ensure_one()

        if not paramiko:
            return {}

        try:
            return self._get_storage_info_impl()
        except Exception as e:
            _logger.error("Failed to get storage info: %s", str(e))
            return {}

    def _get_storage_info_impl(self):
        """Collect backup statistics and remote disk usage."""
        with self._sftp_session() as session:
            # Get backup list (same session, no second connection)
            backups = self._collect_backups(session.sftp)

            # Calculate total backup size
            total_size = sum(backup['size_bytes'] for backup in backups)

            # Try to get disk space information (may not be available on all servers).
            # ``exec_command`` executes through a shell on the remote host, so the
            # remote directory must be shell-quoted to prevent command injection
            # via an admin-controlled ``remote_directory`` value.
            total_space = used_space = free_space = 0
            try:
                quoted_dir = shlex.quote(self.remote_directory or '')
                df_cmd = "df -B1 %s | tail -1" % quoted_dir
                _, stdout, _ = session.client.exec_command(df_cmd)
                if stdout.channel.recv_exit_status() == 0:
                    parts = stdout.read().decode('utf-8', 'replace').split()
                    if len(parts) >= 4:
                        total_space = int(parts[1])
                        used_space = int(parts[2])
                        free_space = int(parts[3])
            except Exception:
                total_space = used_space = free_space = 0

            return {
                'provider_type': 'sftp',
                'backup_count': len(backups),
                'total_backup_size_gb': total_size / (1024**3),
                'free_space_gb': free_space / (1024**3) if free_space else None,
                'total_space_gb': total_space / (1024**3) if total_space else None,
                'used_space_gb': used_space / (1024**3) if used_space else None,
                'storage_path': f"{self.hostname}:{self.remote_directory}",
            }

    def action_reset_host_key(self):
        """Forget the pinned host key (e.g. after a legitimate server migration)."""
        self.ensure_one()
        self.write({
            'server_host_key': False,
            'host_key_fingerprint': False,
        })
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Pinned host key removed',
                'message': (
                    'The next successful connection to %s will pin the new '
                    'server host key. Run "Test Connection" now to pin it.'
                    % self.hostname
                ),
                'type': 'warning',
                'sticky': False,
            }
        }

    # ------------------------------------------------------------------
    # Connection handling
    # ------------------------------------------------------------------
    @contextlib.contextmanager
    def _sftp_session(self):
        """Open an SSH/SFTP session and always clean up afterwards.

        Yields a small object with ``client``, ``sftp`` and
        ``pinned_fingerprint`` attributes. The SSH client is closed and any
        temp file written for the private key is removed on exit, even when
        an error occurs (failing to do so leaks key material into ``/tmp``).
        """
        options, cleanup = self._get_connection_options()
        client = None
        try:
            client = paramiko.SSHClient()
            capture_policy = self._configure_host_key_policy(client)
            client.connect(**options)

            pinned_fingerprint = None
            if capture_policy is not None and capture_policy.key is not None:
                self._pin_host_key(capture_policy.key)
                pinned_fingerprint = self.host_key_fingerprint

            sftp = client.open_sftp()
            if self.transfer_timeout:
                # Apply the configured transfer timeout to the SFTP channel
                # socket, so stalled transfers fail instead of hanging.
                sftp.get_channel().settimeout(self.transfer_timeout)

            try:
                yield types.SimpleNamespace(
                    client=client,
                    sftp=sftp,
                    pinned_fingerprint=pinned_fingerprint,
                )
            finally:
                try:
                    sftp.close()
                except Exception:
                    pass
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
            cleanup()

    def _get_connection_options(self):
        """Build the Paramiko ``connect()`` keyword arguments.

        Returns a ``(options_dict, cleanup_callable)`` tuple. The caller MUST
        invoke ``cleanup_callable()`` in a ``finally`` block to remove any
        temp file written for the SSH private key.
        """
        options = {
            'hostname': self.hostname,
            'port': self.port,
            'username': self.username,
            'timeout': self.connection_timeout or 30,
            'banner_timeout': self.connection_timeout or 30,
            'auth_timeout': self.connection_timeout or 30,
        }

        # List of paths to remove once the caller is done with the options.
        # Filled lazily by the auth section below.
        temp_files = []

        # Authentication
        if self.auth_type == 'key':
            # SSH private key authentication
            if self.private_key:
                # Write key to temp file (Paramiko expects a file path here).
                # Caller is responsible for unlinking it via the returned cleanup.
                key_file = tempfile.NamedTemporaryFile(
                    mode='w', suffix='.key', delete=False
                )
                try:
                    key_file.write(self.private_key)
                finally:
                    key_file.close()
                # Keep the private key readable by the Odoo process only
                os.chmod(key_file.name, 0o600)
                temp_files.append(key_file.name)
                options['key_filename'] = key_file.name
                options['look_for_keys'] = False
                options['allow_agent'] = False
                if self.private_key_passphrase:
                    options['password'] = self.private_key_passphrase
            else:
                # Rely on default ~/.ssh/id_* keys and the SSH agent
                options['look_for_keys'] = True
                options['allow_agent'] = True
        elif self.password:
            options['password'] = self.password
            # Do not silently fall back to keys/agent when a password is set
            options['look_for_keys'] = False
            options['allow_agent'] = False
        else:
            # No credentials configured: keep the historical behaviour of
            # trying default keys / agent.
            options['look_for_keys'] = True
            options['allow_agent'] = True

        def _cleanup():
            """Remove any temp file created for the private key."""
            for path in temp_files:
                try:
                    os.unlink(path)
                except OSError as e:
                    _logger.warning(
                        "Could not remove temp key file %s: %s", path, e,
                    )

        return options, _cleanup

    def _configure_host_key_policy(self, client):
        """Apply the host key policy for this connection.

        Three cases, in order of preference:

        * a key is already pinned -> only that key is accepted (``RejectPolicy``);
        * verification is enabled but nothing is pinned yet -> the key is
          captured and pinned right after a successful connection (TOFU);
        * verification is explicitly disabled -> ``AutoAddPolicy`` (insecure,
          documented as such in the UI).

        Returns the capture policy when the key still has to be learned.
        """
        pinned = (self.server_host_key or '').strip()

        if self.verify_host_key and pinned:
            key = self._parse_pinned_host_key(pinned)
            host_pattern = (
                self.hostname if self.port == 22
                else '[%s]:%d' % (self.hostname, self.port)
            )
            client.get_host_keys().add(host_pattern, key.get_name(), key)
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
            return None

        if self.verify_host_key:
            # First connection (the key gets pinned right after it succeeds)
            policy = _CaptureHostKeyPolicy()
            client.set_missing_host_key_policy(policy)
            return policy

        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        return None

    # Public key type -> Paramiko key class. Paramiko decides the actual
    # algorithm from the key blob, the class only has to match the family.
    _HOST_KEY_CLASSES = {
        'ssh-rsa': 'RSAKey',
        'ssh-dss': 'DSSKey',
        'ssh-ed25519': 'Ed25519Key',
        'ecdsa-sha2-nistp256': 'ECDSAKey',
        'ecdsa-sha2-nistp384': 'ECDSAKey',
        'ecdsa-sha2-nistp521': 'ECDSAKey',
    }

    def _parse_pinned_host_key(self, pinned):
        """Turn the stored public host key line into a Paramiko key object."""
        parts = pinned.split()
        if len(parts) < 2:
            raise UserError(
                "The pinned host key on this provider is invalid. Use "
                "\"Reset Pinned Host Key\" and test the connection again."
            )

        key_type, key_blob = parts[0], parts[1]
        class_name = self._HOST_KEY_CLASSES.get(key_type)
        key_class = getattr(paramiko, class_name, None) if class_name else None
        if key_class is None:
            raise UserError(
                "Unsupported pinned host key type %r. Use \"Reset Pinned Host "
                "Key\" and test the connection again." % key_type
            )

        try:
            return key_class(data=base64.b64decode(key_blob))
        except Exception as e:
            raise UserError(
                "The pinned host key on this provider could not be read (%s). "
                "Use \"Reset Pinned Host Key\" and test the connection again." % e
            )

    def _pin_host_key(self, key):
        """Pin the server host key on the first successful connection (TOFU).

        Returns the key fingerprint.
        """
        if self.server_host_key or not self.verify_host_key:
            return None

        fingerprint = self._key_fingerprint(key)
        self.write({
            'server_host_key': '%s %s' % (key.get_name(), key.get_base64()),
            'host_key_fingerprint': fingerprint,
        })
        _logger.info(
            "Pinned SFTP host key for %s:%s (%s)",
            self.hostname, self.port, fingerprint,
        )
        return fingerprint

    @staticmethod
    def _key_fingerprint(key):
        """Return the SHA-256 fingerprint (same format AsyncSSH used)."""
        digest = hashlib.sha256(key.asbytes()).digest()
        return 'SHA256:' + base64.b64encode(digest).decode('ascii').rstrip('=')

    @staticmethod
    def _get_server_version(client):
        """Best-effort read of the remote SSH banner."""
        try:
            transport = client.get_transport()
            if transport and transport.remote_version:
                return transport.remote_version
        except Exception:
            pass
        return "SSH server (version unknown)"

    @staticmethod
    def _is_missing_path_error(error):
        """Return True when the error means 'remote path does not exist'.

        Paramiko reports a missing SFTP path as ``FileNotFoundError`` or as a
        bare ``IOError``/``OSError`` depending on the version, so all three
        shapes are handled here.
        """
        if isinstance(error, FileNotFoundError):
            return True
        if getattr(error, 'errno', None) == errno.ENOENT:
            return True
        return 'no such file' in str(error).lower()

    def _ensure_remote_directory(self, sftp, path):
        """Create the remote directory (and its missing parents).

        Paramiko has no ``makedirs(..., exist_ok=True)`` equivalent, so the
        path is walked component by component and only what is missing gets
        created.
        """
        path = (path or '').strip()
        if not path:
            return

        is_absolute = path.startswith('/')
        parts = [part for part in path.split('/') if part and part != '.']
        current = '/' if is_absolute else ''

        for part in parts:
            if part == '..':
                raise UserError(
                    "Invalid remote directory: '..' is not allowed (%r)" % path
                )
            current = os.path.join(current, part) if current else part
            try:
                sftp.stat(current)
            except (IOError, OSError) as e:
                if not self._is_missing_path_error(e):
                    raise
                sftp.mkdir(current)

    def _describe_connection_error(self, error):
        """Return a user-friendly error message.

        Authentication failures, timeouts and host key validation failures get
        explicit guidance instead of the raw Paramiko error, because the right
        reaction (fix the credentials, check connectivity vs. reset the pinned
        key or investigate a possible attack) is not obvious to end users.
        """
        if paramiko is not None:
            if isinstance(error, TimeoutError):
                return "Connection timeout. Server may be unreachable."
            if isinstance(error, paramiko.AuthenticationException):
                return "Authentication failed. Check username/password or key."

        error_text = str(error).lower()
        is_host_key_issue = (
            (paramiko is not None and isinstance(error, paramiko.BadHostKeyException))
            or 'host key' in error_text
            or 'known_hosts' in error_text
        )
        if is_host_key_issue and self.server_host_key:
            return (
                "SECURITY WARNING: the host key presented by %s:%s does not "
                "match the key pinned on the first connection (fingerprint %s). "
                "The connection was aborted. If the SFTP server was "
                "legitimately reinstalled or migrated, use 'Reset Pinned Host "
                "Key' on the provider and test the connection again. "
                "Otherwise, this connection may be intercepted by an attacker."
                % (self.hostname, self.port, self.host_key_fingerprint or 'unknown')
            )
        if 'permission denied' in error_text or 'errno 13' in error_text:
            return (
                "Permission denied by the SFTP server while using Remote "
                "Directory '%s'. Check that the directory exists, is writable "
                "by the SFTP user, and that the field is filled in (provider "
                "form: Storage Settings > Directory Configuration). "
                "Server said: %s" % (self.remote_directory or '(not set)', error)
            )
        return str(error)

    def _is_backup_file(self, filename):
        """Check if filename is a backup file."""
        backup_extensions = ['.zip', '.dump', '.sql', '.tar.gz', '.tgz']
        return any(filename.lower().endswith(ext) for ext in backup_extensions)

    # Validation methods
    @api.constrains('hostname')
    def _check_hostname(self):
        """Validate hostname."""
        for record in self:
            if not record.hostname:
                raise ValidationError("Hostname is required")

    @api.constrains('port')
    def _check_port(self):
        """Validate port number."""
        for record in self:
            if not (1 <= record.port <= 65535):
                raise ValidationError("Port must be between 1 and 65535")

    @api.constrains('connection_timeout', 'transfer_timeout')
    def _check_timeouts(self):
        """Validate timeout values."""
        for record in self:
            if record.connection_timeout < 1:
                raise ValidationError("Connection timeout must be at least 1 second")
            if record.transfer_timeout < 0:
                raise ValidationError("Transfer timeout must be non-negative")
