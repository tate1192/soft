# -*- coding: utf-8 -*-

import hashlib
import os
import shutil
import datetime
import errno
import logging

from odoo import models, fields, api
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)


class BackupProviderLocal(models.Model):
    """
    Local filesystem backup provider.
    
    Stores backups in the local filesystem with support for
    directory organization, cleanup, and verification.
    """
    _name = 'backup.provider.local'
    _description = 'Local Backup Provider'
    _inherit = 'backup.provider.abstract'
    
    provider_type = fields.Selection(
        selection=[('local', 'Local Storage')],
        default='local',
        readonly=True
    )
    
    # Local-specific configuration
    backup_directory = fields.Char(
        string='Backup Directory',
        required=True,
        default='/home/backups',
        help='Full path to the directory where backups will be stored'
    )
    create_subdirectories = fields.Boolean(
        string='Create Subdirectories',
        default=False,
        help='Create subdirectories by date for better organization'
    )
    directory_pattern = fields.Selection([
        ('yyyy-mm-dd', 'YYYY-MM-DD'),
        ('yyyy/mm', 'YYYY/MM'),
        ('yyyy/mm/dd', 'YYYY/MM/DD'),
        ('none', 'No Subdirectories'),
    ], string='Directory Pattern', default='yyyy-mm-dd')
    max_directory_size_gb = fields.Float(
        string='Max Directory Size (GB)',
        default=100.0,
        help='Maximum size allowed for the backup directory (0 = unlimited)'
    )
    check_disk_space = fields.Boolean(
        string='Check Disk Space',
        default=True,
        help='Check available disk space before creating backups'
    )
    min_free_space_gb = fields.Float(
        string='Minimum Free Space (GB)',
        default=5.0,
        help='Minimum free disk space required to create backups'
    )
    
    # Override abstract methods
    def test_connection(self):
        """Test local directory access and permissions."""
        self.ensure_one()
        
        try:
            # Check if directory exists
            if not os.path.exists(self.backup_directory):
                # Try to create it (owner-only: it will hold full DB dumps)
                os.makedirs(self.backup_directory, mode=0o700, exist_ok=True)
                message = "Directory created successfully: %s" % self.backup_directory
            else:
                message = "Directory exists and is accessible: %s" % self.backup_directory
            
            # Check write permissions
            test_file = os.path.join(self.backup_directory, '.write_test')

            try:
                with open(test_file, 'w') as f:
                    f.write('test')

                os.remove(test_file)
                message += "\n" + "Write permissions: OK"
            except (IOError, OSError) as e:
                raise UserError("No write permission in directory: %s" % str(e))
            
            # Check disk space
            if self.check_disk_space:
                free_space = self._get_free_disk_space()
                message += "\n" + "Available disk space: %.2f GB" % free_space
                
                if free_space < self.min_free_space_gb:
                    raise UserError(
                        "Insufficient disk space. Available: %.2f GB, Required: %.2f GB" 
                        % (free_space, self.min_free_space_gb)
                    )
            
            return {
                'success': True,
                'message': message
            }
        except Exception as e:
            error_msg = "Connection test failed: %s" % str(e)
            
            return {
                'success': False,
                'message': error_msg
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

    def upload_backup(self, backup_file_path, remote_filename):
        """Copy backup file to local directory."""
        self.ensure_one()
        
        try:
            remote_filename = self._sanitize_remote_filename(remote_filename)
            # Determine target directory
            target_dir = self._get_target_directory()
            
            # Ensure target directory exists. Backups are full database dumps,
            # so keep both the directory and the files owner-only (0700/0600):
            # a 0755 dir + 0644 file (copy2 keeps the source mode) would make
            # every dump world-readable on the host.
            os.makedirs(target_dir, mode=0o700, exist_ok=True)
            
            # Check available space
            if self.check_disk_space:
                backup_size = os.path.getsize(backup_file_path) / (1024**3)  # Convert to GB
                free_space = self._get_free_disk_space()
                
                if free_space - backup_size < self.min_free_space_gb:
                    raise UserError(
                        "Insufficient disk space for backup. Required: %.2f GB, Available: %.2f GB" 
                        % (backup_size + self.min_free_space_gb, free_space)
                    )
            
            # Copy file to target directory
            target_path = os.path.join(target_dir, remote_filename)
            
            shutil.copy2(backup_file_path, target_path)
            # copy2 preserves the source mode (0644 by default); tighten it.
            os.chmod(target_path, 0o600)
            
            # Verify the copy
            if not self._verify_backup_integrity(target_path, backup_file_path):
                raise UserError("Backup verification failed after upload")
            
            # Get file stats
            file_stats = os.stat(target_path)
            
            return {
                'success': True,
                'message': "Backup uploaded successfully to: %s" % target_path,
                'metadata': {
                    'file_path': target_path,
                    'size_bytes': file_stats.st_size,
                    'size_gb': file_stats.st_size / (1024**3),
                    'created_date': datetime.datetime.fromtimestamp(file_stats.st_ctime),
                    'modified_date': datetime.datetime.fromtimestamp(file_stats.st_mtime),
                }
            }
        except Exception as e:
            return {
                'success': False,
                'message': self._describe_upload_error(e),
                'metadata': {}
            }
    
    def download_backup(self, remote_filename, local_path):
        """Copy backup file from local directory to specified path."""
        self.ensure_one()
        
        try:
            # Find the backup file
            backup_file_path = self._find_backup_file(remote_filename)
            
            if not backup_file_path or not os.path.exists(backup_file_path):
                raise UserError("Backup file not found: %s" % remote_filename)
            
            # Copy to target location
            shutil.copy2(backup_file_path, local_path)
            
            # Verify the copy
            if not self._verify_backup_integrity(local_path, backup_file_path):
                raise UserError("Backup verification failed after download")
            
            return {
                'success': True,
                'message': "Backup downloaded successfully to: %s" % local_path
            }
        except Exception as e:
            return {
                'success': False,
                'message': "Download failed: %s" % str(e)
            }
    
    def list_backups(self, prefix=None):
        """List backup files in local directory."""
        self.ensure_one()
        
        try:
            backups = []
            
            # Search in all subdirectories if using directory pattern
            search_dirs = [self.backup_directory]
            if self.create_subdirectories and self.directory_pattern != 'none':
                # Add common subdirectories
                for root, dirs, files in os.walk(self.backup_directory):
                    search_dirs.extend([os.path.join(root, d) for d in dirs])
            
            for search_dir in search_dirs:
                if not os.path.exists(search_dir):
                    continue
                    
                for filename in os.listdir(search_dir):
                    if prefix and not filename.startswith(prefix):
                        continue
                        
                    file_path = os.path.join(search_dir, filename)
                    if os.path.isfile(file_path) and self._is_backup_file(filename):
                        file_stats = os.stat(file_path)
                        
                        backups.append({
                            'filename': filename,
                            'full_path': file_path,
                            'size_bytes': file_stats.st_size,
                            'size_gb': file_stats.st_size / (1024**3),
                            'created_date': datetime.datetime.fromtimestamp(file_stats.st_ctime),
                            'modified_date': datetime.datetime.fromtimestamp(file_stats.st_mtime),
                        })
            
            # Sort by creation date (newest first)
            backups.sort(key=lambda x: x['created_date'], reverse=True)
            
            return backups
        except Exception as e:
            _logger.error("Failed to list backups: %s", str(e))
            return []
    
    def delete_backup(self, remote_filename):
        """Delete backup file from local directory."""
        self.ensure_one()
        
        try:
            backup_file_path = self._find_backup_file(remote_filename)
            
            if not backup_file_path or not os.path.exists(backup_file_path):
                return {
                    'success': False,
                    'message': "Backup file not found: %s" % remote_filename
                }
            
            os.remove(backup_file_path)
            
            return {
                'success': True,
                'message': "Backup file deleted successfully: %s" % backup_file_path
            }
        except Exception as e:
            return {
                'success': False,
                'message': "Delete failed: %s" % str(e)
            }
    
    def get_storage_info(self):
        """Get local storage information."""
        self.ensure_one()
        
        try:
            # Get directory size
            total_size = self._get_directory_size(self.backup_directory)
            
            # Get disk space
            free_space = self._get_free_disk_space()
            disk_usage = shutil.disk_usage(self.backup_directory)
            
            return {
                'provider_type': 'local',
                'backup_count': len(self.list_backups()),
                'total_backup_size_gb': total_size / (1024**3),
                'free_space_gb': free_space,
                'total_space_gb': disk_usage.total / (1024**3),
                'used_space_gb': (disk_usage.total - disk_usage.free) / (1024**3),
                'storage_path': self.backup_directory,
            }
        except Exception as e:
            _logger.error("Failed to get storage info: %s", str(e))
            return {}
    
    # Private utility methods
    
    def _get_target_directory(self):
        """Get the target directory for new backups."""
        if not self.create_subdirectories or self.directory_pattern == 'none':
            return self.backup_directory
        
        now = datetime.datetime.now()
        
        if self.directory_pattern == 'yyyy-mm-dd':
            subdir = now.strftime('%Y-%m-%d')
        elif self.directory_pattern == 'yyyy/mm':
            subdir = now.strftime('%Y/%m')
        elif self.directory_pattern == 'yyyy/mm/dd':
            subdir = now.strftime('%Y/%m/%d')
        else:
            subdir = ''
        
        return os.path.join(self.backup_directory, subdir)

    def _get_destination_path(self):
        """Return the directory new backups are written to (used in job logs)."""
        return self._get_target_directory()

    def _preflight_check(self):
        """Verify the destination directory is usable before the dump runs.

        A dump can take minutes and produce gigabytes, so finding out only
        afterwards that the target directory is not writable wastes all of it.
        The check walks up to the closest existing parent (nothing is created
        here — the upload itself creates the directory) and writes/removes a
        probe file to prove the Odoo user really can write there.
        """
        self.ensure_one()

        target_dir = self._get_target_directory()
        probe_dir = target_dir
        while probe_dir and not os.path.isdir(probe_dir):
            parent = os.path.dirname(probe_dir.rstrip('/')) or '/'
            if parent == probe_dir:
                break
            probe_dir = parent

        if not os.path.isdir(probe_dir):
            return {
                'success': False,
                'message': self._destination_problem_message(
                    target_dir,
                    "the directory does not exist and no existing parent "
                    "directory could be used to create it"),
            }

        target_exists = os.path.isdir(target_dir)
        probe_file = os.path.join(probe_dir, '.backup_versatile_write_test')
        try:
            with open(probe_file, 'w') as handle:
                handle.write('write test')
            os.remove(probe_file)
        except OSError as error:
            if target_exists:
                reason = "the directory exists but is not writable by the Odoo user (%s)" % error
            else:
                reason = ("the directory does not exist and cannot be created: "
                          "the closest existing parent '%s' is not writable by "
                          "the Odoo user (%s)" % (probe_dir, error))
            return {
                'success': False,
                'message': self._destination_problem_message(target_dir, reason),
            }

        if self.check_disk_space:
            try:
                free_space = shutil.disk_usage(probe_dir).free / (1024 ** 3)
            except OSError:
                free_space = None
            if free_space is not None and free_space < self.min_free_space_gb:
                return {
                    'success': False,
                    'message': (
                        "Not enough free space on the disk that holds '%s': "
                        "%.2f GB available, minimum configured is %.2f GB. "
                        "Free up space or lower 'Minimum Free Space (GB)'."
                        % (target_dir, free_space, self.min_free_space_gb)
                    ),
                }

        return {
            'success': True,
            'message': 'Target directory is writable: %s' % target_dir,
        }

    def _destination_problem_message(self, target_dir, reason):
        """Build an actionable message for an unusable backup directory.

        A bare ``[Errno 13] Permission denied: '/home/backups'`` tells the
        operator nothing about which field to fix — and because the module
        ships ``/home/backups`` as the field default, an untouched field
        surfaces exactly like that.
        """
        return (
            "Backup Directory '%s' cannot be used: %s. Set a directory the "
            "Odoo user can write to in the provider form (Storage Settings > "
            "Directory Configuration)." % (target_dir, reason)
        )

    def _describe_upload_error(self, error):
        """Turn an upload failure into a message that names the field to fix."""
        if isinstance(error, OSError) and error.errno in (
            errno.EACCES, errno.EPERM, errno.EROFS, errno.ENOENT, errno.ENOTDIR,
        ):
            return self._destination_problem_message(
                self._get_target_directory(), str(error))
        return "Upload failed: %s (Backup Directory: %s)" % (
            error, self._get_target_directory())

    def _find_backup_file(self, filename):
        """Find a backup file in the directory structure."""
        filename = self._sanitize_remote_filename(filename)
        # First check the root directory
        file_path = os.path.join(self.backup_directory, filename)
        if os.path.exists(file_path):
            return file_path
        
        # Search in subdirectories
        for root, dirs, files in os.walk(self.backup_directory):
            if filename in files:
                return os.path.join(root, filename)
        
        return None
    
    def _is_backup_file(self, filename):
        """Check if filename is a backup file."""
        backup_extensions = ['.zip', '.dump', '.sql', '.tar.gz', '.tgz']
        return any(filename.lower().endswith(ext) for ext in backup_extensions)
    
    def _get_free_disk_space(self):
        """Get free disk space in GB."""
        disk_usage = shutil.disk_usage(self.backup_directory)
        return disk_usage.free / (1024**3)
    
    def _get_directory_size(self, directory):
        """Get total size of directory in bytes."""
        total_size = 0

        for dirpath, dirnames, filenames in os.walk(directory):
            for filename in filenames:
                filepath = os.path.join(dirpath, filename)

                if os.path.isfile(filepath):
                    total_size += os.path.getsize(filepath)

        return total_size
    
    def _verify_backup_integrity(self, file1, file2):
        """Verify that two files are identical.

        Fails closed: any exception during hash computation returns False
        so a verification we could not actually perform never lets a corrupt
        or partial backup through as "verified".
        """
        def get_file_hash(filepath):
            # SHA-256 (MD5 is collision-vulnerable and unsuitable for
            # integrity verification of database backups).
            hash_sha = hashlib.sha256()
            with open(filepath, "rb") as f:
                for chunk in iter(lambda: f.read(64 * 1024), b""):
                    hash_sha.update(chunk)
            return hash_sha.hexdigest()

        try:
            return get_file_hash(file1) == get_file_hash(file2)
        except Exception as e:
            _logger.warning(
                "Backup integrity verification failed (failing closed): %s",
                e,
            )
            return False
    
    # Validation methods
    @api.constrains('backup_directory')
    def _check_backup_directory(self):
        """Validate backup directory path."""
        for record in self:
            if not record.backup_directory:
                raise ValidationError("Backup directory is required")
            
            if not os.path.isabs(record.backup_directory):
                raise ValidationError("Backup directory must be an absolute path")
    
    @api.constrains('min_free_space_gb')
    def _check_min_free_space(self):
        """Validate minimum free space value."""
        for record in self:
            if record.min_free_space_gb < 0:
                raise ValidationError("Minimum free space must be non-negative")
