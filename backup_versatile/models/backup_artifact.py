# -*- coding: utf-8 -*-

from odoo import fields, models


class BackupArtifact(models.Model):
    """Durable ownership record for a successfully uploaded backup file."""

    _name = 'backup.artifact'
    _description = 'Backup Artifact'
    _order = 'create_date desc, id desc'

    job_id = fields.Many2one(
        'backup.job',
        string='Backup Job',
        ondelete='set null',
        index=True,
    )
    config_id = fields.Many2one(
        'backup.config',
        string='Backup Configuration',
        required=True,
        ondelete='cascade',
        index=True,
    )
    provider_model = fields.Char(
        string='Provider Model',
        required=True,
        index=True,
    )
    provider_id = fields.Integer(
        string='Provider ID',
        required=True,
        index=True,
    )
    filename = fields.Char(
        string='Backup Filename',
        required=True,
        index=True,
    )
    object_path = fields.Char(
        string='Provider Object Path',
        help='Exact local path or remote object key returned by the provider.',
    )
    size_bytes = fields.Integer()
    active = fields.Boolean(
        string='Active',
        default=True,
        help='False after the artifact has been deleted by retention.',
    )
    deleted_date = fields.Datetime()

    _provider_filename_index = models.Index(
        '(provider_model, provider_id, filename, active)',
    )
