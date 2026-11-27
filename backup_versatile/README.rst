=================
 Backup Versatile
=================

Database backups for Odoo 19, scheduled and monitored: a configuration decides
*what* to dump, *where* to send it and *how long* to keep it, and every run
leaves a job record behind. No Enterprise, no external service required for the
basic path.

Configuration
=============

``backup.config`` holds one backup schedule. A configuration names the database,
the format, one or more storage providers, the retention policy, the
notification addresses and whether the archive is encrypted.

Formats
-------

``zip``
    A ZIP archive of the ``pg_dump`` output together with the filestore, so the
    archive is a full restorable copy of the instance.
``dump``
    A PostgreSQL custom-format ``pg_dump`` file: compact, SQL/data only, no
    attachments. Restore it with ``pg_restore``.

Providers
=========

Providers are records, not settings, so one configuration can write the same
backup to several destinations.

``backup.provider.local``
    A directory on the server or on a mounted network share.
``backup.provider.sftp``
    A remote server over SSH/SFTP, driven by `paramiko`_. Password or private
    key authentication, host key verification with trust-on-first-use pinning,
    remote directory creation and an upload size check. Remote paths are
    sanitized before use: traversal, ``.``/``..`` segments, trailing slashes and
    null bytes are rejected (``tests/test_path_safety.py``).
``backup.provider.cloud``
    S3-compatible storage through `rclone`_ (primary engine, detected at
    runtime); an already configured rclone remote can be reused.

.. _paramiko: https://www.paramiko.org/
.. _rclone: https://rclone.org/

Retention
=========

``count``
    Keep the last *N* backups.
``days``
    Keep everything younger than *N* days.
``custom``
    Explicit policy, evaluated by the cleanup cron.

Cleanup runs from its own scheduled action against the configured provider
locations. Use dedicated backup directories or prefixes; do not point a provider
at a directory that contains unrelated files.

Jobs and monitoring
===================

``backup.job`` records one run: start and end time, duration, size, status,
message and the provider it went to. From the job list and the configuration
form the module reports the last run, the last status, the total number of
backups and the success rate; success and failure can each notify a list of
addresses.

With ``verify_backups`` set, the module reopens the produced archive after the run
and checks it, so a truncated backup fails the job instead of silently becoming
the only copy.

Restore
=======

``backup.restore.wizard`` restores one of a provider's archives. It is guarded on
both ends: the SQL it builds is checked so a ``DROP DATABASE`` cannot run without
its matching ``CREATE`` (``tests/test_restore_guard.py``), the archive name is
sanitized the same way the SFTP paths are, and only the *Administrator* group has
access to the wizard at all.

Security
========

Two groups. *User* reads schedules, providers and jobs (read-only ACL on every
model, no wizard). *Administrator* configures schedules and providers and is the
only group that can restore. Credentials and the encryption key live in ordinary
Odoo database columns. Password widgets mask them in the UI; base64 encoding is
not at-rest encryption. Use database encryption and an external key escrow for
production deployments.

Installation
============

::

    pip install "paramiko>=5.0,<6.0" "cryptography>=50.0,<51.0" "pyOpenSSL>=26.4,<27"
    # optional: rclone for the cloud provider; boto3 for the S3 fallback
    odoo-bin -d <database> -i backup_versatile --stop-after-init

The SFTP and encryption providers declare ``paramiko`` and ``cryptography`` as
external dependencies, so the module refuses to install without them. The backup
scheduler ships active, but no backup runs until a ``backup.config`` record is
active and has at least one active provider.

Tests
=====

``tests/`` holds 125 test methods in fourteen files: file filtering,
encryption, path safety, provider preflight, database preflight, the rclone
command line, the restore guard, retention, the paramiko SFTP session, ZIP
verification, notifications, UI configuration and encryption-key management.
They run without a network or a remote server: the SFTP session and rclone are
stubbed.

License
=======

``LGPL-3``, like the rest of this repository — see the ``license`` key in the
manifest and ``README.md`` at the repository root.
