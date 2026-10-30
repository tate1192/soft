# -*- coding: utf-8 -*-

import logging

from abc import abstractmethod
from odoo import models, fields, api
from odoo.exceptions import AccessDenied

_logger = logging.getLogger(__name__)


class BackupProviderAbstract(models.AbstractModel):
    """
    Abstract base class for all backup storage providers.
    
    This class defines the interface that all backup providers must implement.
    It uses the Strategy pattern to allow easy extension with new storage providers.
    """
    _name = 'backup.provider.abstract'
    _description = 'Abstract Backup Provider'
    
    name = fields.Char(
        string='Name',
        required=True,
        help='Human-readable name for this backup provider'
    )
    active = fields.Boolean(
        string='Active',
        default=True,
        help='Whether this provider is available for use'
    )
    test_connection_result = fields.Text(
        string='Last Connection Test',
        readonly=True,
        help='Result of the last connection test'
    )
    test_connection_date = fields.Datetime(
        string='Last Test Date',
        readonly=True
    )
    display_name = fields.Char(
        compute='_compute_display_name',
        store=False,
    )

    @api.depends('name', 'provider_type')
    def _compute_display_name(self):
        """Return name with provider type for better identification."""
        for record in self:
            if record.name and hasattr(record, 'provider_type') and record.provider_type:
                provider_type_field = record._fields.get('provider_type')
                if provider_type_field and hasattr(provider_type_field, 'selection'):
                    provider_type_name = dict(provider_type_field.selection).get(record.provider_type, record.provider_type)
                    record.display_name = "%s (%s)" % (record.name, provider_type_name)
                else:
                    record.display_name = "%s (%s)" % (record.name, record.provider_type)
            elif record.name:
                record.display_name = record.name
            else:
                record.display_name = ("Provider #%s" % record.id) if record.id else "New Provider"

    # Abstract methods that must be implemented by concrete providers
    @api.private
    @abstractmethod
    def test_connection(self):
        """
        Test the connection to the storage provider.
        
        Returns:
            dict: Result containing success status and message
        """
        pass

    def _assert_backup_admin(self):
        """Reject side-effecting provider actions for read-only users."""
        if not self.env.user.has_group('backup_versatile.group_backup_admin'):
            raise AccessDenied(self.env._(
                'Backup administrator rights are required for this operation.'
            ))

    def test_connection_action(self):
        """
        Test connection method for UI button - shows notification and updates fields.
        
        This method calls test_connection() and displays appropriate notifications.
        """
        self._assert_backup_admin()
        try:
            result = self.test_connection()
            
            # Update test connection fields
            self.test_connection_result = result.get('message', 'Unknown result')
            self.test_connection_date = fields.Datetime.now()
            
            # Show notification
            if result.get('success'):
                return {
                    'type': 'ir.actions.client',
                    'tag': 'display_notification',
                    'params': {
                        'title': 'Connection Test Successful',
                        'message': result.get('message', 'Connection test passed'),
                        'type': 'success',
                        'sticky': False,
                    }
                }
            else:
                return {
                    'type': 'ir.actions.client',
                    'tag': 'display_notification',
                    'params': {
                        'title': 'Connection Test Failed',
                        'message': result.get('message', 'Connection test failed'),
                        'type': 'danger',
                        'sticky': True,
                    }
                }
        except Exception as e:
            # Update fields on exception
            error_msg = f"Connection test failed: {str(e)}"
            self.test_connection_result = error_msg
            self.test_connection_date = fields.Datetime.now()
            
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Connection Test Error',
                    'message': error_msg,
                    'type': 'danger',
                    'sticky': True,
                }
            }
    
    @api.private
    @abstractmethod
    def upload_backup(self, backup_file_path, remote_filename):
        """
        Upload a backup file to the storage provider.
        
        Args:
            backup_file_path (str): Local path to the backup file
            remote_filename (str): Name to use for the remote file
            
        Returns:
            dict: Result containing success status, message, and metadata
        """
        pass
    
    @api.private
    @abstractmethod
    def download_backup(self, remote_filename, local_path):
        """
        Download a backup file from the storage provider.
        
        Args:
            remote_filename (str): Name of the remote file
            local_path (str): Local path where to save the file
            
        Returns:
            dict: Result containing success status and message
        """
        pass
    
    @api.private
    @abstractmethod
    def list_backups(self, prefix=None):
        """
        List available backup files in the storage provider.
        
        Args:
            prefix (str, optional): Filter files by prefix
            
        Returns:
            list: List of backup file information dicts
        """
        pass
    
    @api.private
    @abstractmethod
    def delete_backup(self, remote_filename):
        """
        Delete a backup file from the storage provider.
        
        Args:
            remote_filename (str): Name of the remote file to delete
            
        Returns:
            dict: Result containing success status and message
        """
        pass
    
    @api.private
    @abstractmethod
    def get_storage_info(self):
        """
        Get storage information (used space, available space, etc.).
        
        Returns:
            dict: Storage information
        """
        pass

    def _get_destination_path(self):
        """Return a human readable destination, used in the job log.

        Concrete providers override this so a failed/successful upload line
        names the *real* target (e.g. the resolved directory) instead of only
        the provider label, which may say nothing about where files land.
        Providers without a single path (cloud buckets) keep it empty.
        """
        return ''

    def _preflight_check(self):
        """Verify the destination is usable *before* the database dump.

        Returning ``{'success': False, 'message': ...}`` aborts the job before
        any dump work happens: producing a multi-gigabyte dump and only then
        discovering an unwritable directory or an unreachable server wastes
        minutes and disk space. The default is a no-op so providers without a
        cheap check keep working unchanged.
        """
        self.ensure_one()
        return {'success': True, 'message': ''}
