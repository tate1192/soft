import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)

# Rename cron jobs: "Ultimate Backup Lite" -> "Versatile Backup" branding.
# These ir.cron records are noupdate=1, so the XML rename alone does not
# touch existing DB rows. This migration rewrites the `name` field directly.
_CRON_RENAMES = {
    'backup_versatile.backup_cron': 'Database Versatile Backup Scheduler',
    'backup_versatile.backup_cleanup_cron': 'Database Versatile Backup Cleanup',
    'backup_versatile.backup_temp_sweep_cron': 'Database Versatile Backup - Stale Temp Sweep',
}

# Replacement `code` for the cleanup cron's server action: the original
# silently swallowed every exception (``except Exception as e: pass``), which
# masked retention failures. noupdate=1 means the XML change does not reach
# existing DB rows, so we patch it here too.
_CLEANUP_CRON_CODE = """# Clean up old backups for all active configurations
configs = model.search([('active', '=', True)])
for config in configs:
    try:
        config.with_context(manual_execution=False).cleanup_old_backups()
    except Exception:
        log.exception("Cleanup failed for backup config '%s' (id=%s)", config.name, config.id)
"""

_CRON_CODE_UPDATES = {
    'backup_versatile.backup_cleanup_cron': _CLEANUP_CRON_CODE,
}


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})

    # 1. Rename crons
    for xmlid, new_name in _CRON_RENAMES.items():
        cron = env.ref(xmlid, raise_if_not_found=False)
        if not cron:
            _logger.warning("backup_versatile rename: cron %s not found, skipping", xmlid)
            continue
        if cron.name != new_name:
            _logger.info(
                "backup_versatile rename: %s '%s' -> '%s'",
                xmlid, cron.name, new_name,
            )
            cron.write({'name': new_name})

    # 2. Patch cron server-action code (was silently swallowing errors)
    for xmlid, new_code in _CRON_CODE_UPDATES.items():
        cron = env.ref(xmlid, raise_if_not_found=False)
        if not cron:
            continue
        if cron.code != new_code:
            cron.write({'code': new_code})
            _logger.info("backup_versatile: updated code for cron %s", xmlid)
