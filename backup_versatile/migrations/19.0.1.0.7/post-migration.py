import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Reconcile scheduled actions for existing installations."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    desired_states = {
        'backup_versatile.backup_cron': True,
        'backup_versatile.backup_cleanup_cron': True,
        'backup_versatile.backup_temp_sweep_cron': True,
    }
    for xmlid, active in desired_states.items():
        cron = env.ref(xmlid, raise_if_not_found=False)
        if cron and cron.active != active:
            cron.write({'active': active})
            _logger.info('backup_versatile: set %s active=%s', xmlid, active)
