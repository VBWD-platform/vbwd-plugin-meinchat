"""Meinchat server-retention prune scheduler (S28.1).

Mirrors the booking + subscription plugin schedulers: `on_enable` starts
it (guarded against TESTING in the plugin lifecycle so it never spawns in
the ~1900-test run and exhausts PG connection slots — the lesson from the
booking + subscription scheduler post-mortem). Nothing in core references
it.

The job hard-deletes message rows past `messages_retention_days_server`
plus their attachment objects, daily at 03:00 UTC (cron overridable via
the `retention_prune_cron` config key).
"""
import logging

logger = logging.getLogger(__name__)

_DEFAULT_PRUNE_CRON = "0 3 * * *"  # daily at 03:00 UTC


def run_retention_prune(app):
    """Prune attachments then message rows. Imports are local so importing
    this module never pulls the repositories at startup (apscheduler invokes
    the job inside the worker thread under an app context)."""
    with app.app_context():
        from vbwd.extensions import db
        from plugins.meinchat.meinchat.repositories.message_repository import (
            MessageRepository,
        )
        from plugins.meinchat.meinchat.services.retention_policy import (
            ConfigRetentionPolicy,
        )
        from plugins.meinchat.meinchat.services.retention_service import (
            RetentionService,
        )

        config_store = getattr(app, "config_store", None)

        def _config_provider():
            if config_store is None:
                return {}
            return config_store.get_config("meinchat") or {}

        service = RetentionService(
            message_repo=MessageRepository(db.session),
            attachment_storage=_resolve_storage(app),
            retention_policy=ConfigRetentionPolicy(config_provider=_config_provider),
        )
        # Attachments first (best-effort), then the rows (source of truth).
        attachment_result = service.prune_attachments()
        message_result = service.prune_messages()
        db.session.commit()
        logger.info(
            "[meinchat] retention prune: deleted=%d skipped=%d "
            "skipped_undelivered=%d attachment_errors=%d",
            message_result.deleted_count,
            message_result.skipped_count,
            message_result.skipped_undelivered_count,
            attachment_result.errors,
        )


def _resolve_storage(app):
    from vbwd.interfaces.file_storage import LocalFileStorage

    return LocalFileStorage(
        base_path=app.config.get("UPLOADS_BASE_PATH", "/app/uploads"),
        base_url=app.config.get("UPLOADS_BASE_URL", "/uploads"),
    )


def start_retention_scheduler(app, cron_expression=_DEFAULT_PRUNE_CRON):
    """Start the daily retention-prune job from a 5-field cron expression."""
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        run_retention_prune,
        CronTrigger.from_crontab(cron_expression, timezone="UTC"),
        args=[app],
        id="meinchat_retention_prune",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("[meinchat] Retention scheduler started (cron=%s UTC)", cron_expression)
    return scheduler
