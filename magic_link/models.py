from __future__ import annotations

import datetime
import uuid
from typing import Optional

from django.conf import settings
from django.contrib.auth import login
from django.db import models
from django.http import HttpRequest
from django.urls import reverse
from django.utils import timezone

from .settings import DEFAULT_EXPIRY


def parse_remote_addr(request: HttpRequest) -> str:
    """Extract client IP from request."""
    x_forwarded_for = request.headers.get("X-Forwarded-For", "")
    if x_forwarded_for:
        return x_forwarded_for.split(",")[0]
    return request.META.get("REMOTE_ADDR", "")


def parse_ua_string(request: HttpRequest) -> str:
    """Extract client user-agent from request."""
    return request.headers.get("User-Agent", "")


def link_expires_at(interval: int = DEFAULT_EXPIRY) -> datetime.datetime:
    """Return timestamp used as default link exires_at value."""
    return timezone.now() + datetime.timedelta(seconds=interval)


class InvalidTokenUse(Exception):
    pass


class MagicLink(models.Model):
    """A unique token used for magic links."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="magic_links"
    )
    token = models.UUIDField(
        default=uuid.uuid4, editable=False, unique=True, help_text="Unique login token"
    )
    redirect_to = models.CharField(
        help_text="URL to which user will be redirected after logging in. ('/')",
        max_length=255,
        default="/",
    )
    created_at = models.DateTimeField(
        default=timezone.now, help_text="When the token was originally created"
    )
    expires_at = models.DateTimeField(
        help_text="When the token is due to expire (uses DEFAULT_EXPIRY)",
        default=link_expires_at,
    )
    is_active = models.BooleanField(
        default=True, help_text="Set to False to deactivate the token"
    )

    def __str__(self) -> str:
        return f"Magic link ({self.id}) for {self.user.username}"

    def __repr__(self) -> str:
        return f"<MagicLink id={self.id} user_id={self.user_id} token='{self.token}'>'"

    def get_absolute_url(self) -> str:
        return reverse("use_magic_link", kwargs={"token": self.token})

    @property
    def has_expired(self) -> Optional[bool]:
        """Return True if the token is past its expiry timestamp."""
        if self.expires_at:
            return self.expires_at < timezone.now()
        return None

    @property
    def is_valid(self) -> bool:
        """Return False if link has expired or been marked as inactive."""
        return self.is_active and not self.has_expired

    def validate(self, request: HttpRequest) -> None:
        """
        Raise InvalidToken with appropriate message if token is not valid.

        In addition to the link itself, this method checks that the HttpRequest is
        valid - which means that it is either unauthenticated, or authenticated as
        the user to whom this link refers. You cannot use a magic link to log in as
        someone else if you are already logged in.

        """
        if not self.is_active:
            raise InvalidTokenUse("Link is inactive")
        if self.has_expired:
            raise InvalidTokenUse("Link has expired")
        if request.user.is_anonymous:
            return
        if request.user != self.user:
            raise InvalidTokenUse("Request to use token by another user")

    def log_use(self, request: HttpRequest, status_code: int) -> MagicLinkUse:
        """Create a MagicLinkUse from an HtttpRequest."""
        return MagicLinkUse.objects.create(
            link=self,
            timestamp=timezone.now(),
            http_method=request.method,
            remote_addr=parse_remote_addr(request),
            ua_string=parse_ua_string(request),
            session_key=request.session.session_key or "",
            link_is_valid=self.is_valid,
            http_status_code=status_code,
        )

    def login(self, request: HttpRequest) -> None:
        """Call login as the link.user."""
        login(request, self.user)

    def disable(self) -> None:
        """Disable the link (no further uses)."""
        self.is_active = False
        self.expires_at = timezone.now()
        self.save()


class MagicLinkUse(models.Model):
    """
    Records the use of a token.

    This model is used for auditing purposes - tracking when the token was
    used, recording the IP address and User-Agent of the client, and their
    session id. This allows us to perform diagnostics when tokens fail.

    The canonical pattern of use is to have two records per token - the first
    being a GET to render the login page, and the second being a POST to log
    the user in.

    """

    link = models.ForeignKey(MagicLink, on_delete=models.CASCADE)
    timestamp = models.DateTimeField(
        help_text="When the token page was requested", default=timezone.now
    )
    http_method = models.CharField(max_length=10,)
    session_key = models.CharField(
        max_length=40, help_text="The request session identifier", blank=True
    )
    remote_addr = models.CharField(
        max_length=100,
        blank=True,
        help_text="The client IP address, extracted from HttpRequest",
    )
    ua_string = models.TextField(
        help_text="The client User-Agent, extracted from HttpRequest headers",
        blank=True,
    )
    link_is_valid = models.BooleanField(
        help_text=("Snapshot of parent link is_valid property at the time of use"),
        default=True,
    )
    http_status_code = models.IntegerField(
        help_text=("The HTTP response status code"), default=0,
    )

    class Meta:
        get_latest_by = ("timestamp",)

    def __str__(self) -> str:
        return f"Magic link ({self.link_id}) used at {self.timestamp}"

    def __repr__(self) -> str:
        return (
            f"<MagicLinkUse id={self.id} link_id={self.link_id} "
            f"timestamp='{self.timestamp}''>"
        )
