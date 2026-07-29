"""Optional outbound mail for enterprise invites.

Mail is a convenience, never a dependency: if `[smtp]` is absent from
pm_studio_local/config.toml - or sending fails - the caller falls back to handing the
admin a copyable invite link, which works on a machine with no mail server at all.
That is why `send_invite` returns a bool instead of raising.
"""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage

from .config import Config, SmtpConfig

_TIMEOUT_SECONDS = 20

_BODY = """\
{inviter} invited you to {project} on PM Studio.

Set your password and sign in here:

{url}

This link expires in 7 days. If you weren't expecting it, ignore this message.
"""


def build_invite_message(
    smtp: SmtpConfig, project: str, to_address: str, inviter: str, url: str
) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = f"You've been invited to {project} on PM Studio"
    message["From"] = smtp.from_address
    message["To"] = to_address
    message.set_content(
        _BODY.format(inviter=inviter or "An admin", project=project, url=url)
    )
    return message


def send_invite(config: Config, to_address: str, inviter: str, url: str) -> bool:
    """Returns True if the invite was handed to an SMTP server, False if mail is not
    configured or the attempt failed. A False result is not an error - it means the
    admin shares the link manually."""
    smtp = config.smtp
    if smtp is None or not smtp.is_usable:
        return False
    message = build_invite_message(
        smtp, config.project_name, to_address, inviter, url
    )
    try:
        with smtplib.SMTP(smtp.host, smtp.port, timeout=_TIMEOUT_SECONDS) as client:
            if smtp.use_tls:
                client.starttls(context=ssl.create_default_context())
            if smtp.username:
                client.login(smtp.username, smtp.password)
            client.send_message(message)
        return True
    except (OSError, smtplib.SMTPException):
        # Deliberately broad-but-bounded: any transport or protocol failure degrades to
        # the copyable link rather than failing the invite the admin just created.
        return False
