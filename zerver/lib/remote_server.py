import logging
import urllib
from typing import Any, Dict, List, Mapping, Tuple, Union

import orjson
import requests
from django.conf import settings
from django.forms.models import model_to_dict
from django.utils.translation import gettext as _
from pydantic import BaseModel, ConfigDict

from analytics.models import InstallationCount, RealmCount
from version import ZULIP_VERSION
from zerver.lib.exceptions import JsonableError
from zerver.lib.export import floatify_datetime_fields
from zerver.lib.outgoing_http import OutgoingSession
from zerver.models import Realm, RealmAuditLog


class PushBouncerSession(OutgoingSession):
    def __init__(self) -> None:
        super().__init__(role="push_bouncer", timeout=30)


class PushNotificationBouncerError(Exception):
    pass


class PushNotificationBouncerRetryLaterError(JsonableError):
    http_status_code = 502


class RealmDataForAnalytics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    host: str
    url: str
    date_created: float
    deactivated: bool

    uuid: str
    uuid_owner_secret: str


def send_to_push_bouncer(
    method: str,
    endpoint: str,
    post_data: Union[bytes, Mapping[str, Union[str, int, None, bytes]]],
    extra_headers: Mapping[str, str] = {},
) -> Dict[str, object]:
    """While it does actually send the notice, this function has a lot of
    code and comments around error handling for the push notifications
    bouncer.  There are several classes of failures, each with its own
    potential solution:

    * Network errors with requests.request.  We raise an exception to signal
      it to the callers.

    * 500 errors from the push bouncer or other unexpected responses;
      we don't try to parse the response, but do make clear the cause.

    * 400 errors from the push bouncer.  Here there are 2 categories:
      Our server failed to connect to the push bouncer (should throw)
      vs. client-side errors like an invalid token.

    """
    assert settings.PUSH_NOTIFICATION_BOUNCER_URL is not None
    assert settings.ZULIP_ORG_ID is not None
    assert settings.ZULIP_ORG_KEY is not None
    url = urllib.parse.urljoin(
        settings.PUSH_NOTIFICATION_BOUNCER_URL, "/api/v1/remotes/" + endpoint
    )
    api_auth = requests.auth.HTTPBasicAuth(settings.ZULIP_ORG_ID, settings.ZULIP_ORG_KEY)

    headers = {"User-agent": f"ZulipServer/{ZULIP_VERSION}"}
    headers.update(extra_headers)

    try:
        res = PushBouncerSession().request(
            method, url, data=post_data, auth=api_auth, verify=True, headers=headers
        )
    except (
        requests.exceptions.Timeout,
        requests.exceptions.SSLError,
        requests.exceptions.ConnectionError,
    ) as e:
        raise PushNotificationBouncerRetryLaterError(
            f"{type(e).__name__} while trying to connect to push notification bouncer"
        )

    if res.status_code >= 500:
        # 500s should be resolved by the people who run the push
        # notification bouncer service, and they'll get an appropriate
        # error notification from the server. We raise an exception to signal
        # to the callers that the attempt failed and they can retry.
        error_msg = "Received 500 from push notification bouncer"
        logging.warning(error_msg)
        raise PushNotificationBouncerRetryLaterError(error_msg)
    elif res.status_code >= 400:
        # If JSON parsing errors, just let that exception happen
        result_dict = orjson.loads(res.content)
        msg = result_dict["msg"]
        if "code" in result_dict and result_dict["code"] == "INVALID_ZULIP_SERVER":
            # Invalid Zulip server credentials should email this server's admins
            raise PushNotificationBouncerError(
                _("Push notifications bouncer error: {error}").format(error=msg)
            )
        elif (
            endpoint == "push/test_notification"
            and "code" in result_dict
            and result_dict["code"] == "INVALID_REMOTE_PUSH_DEVICE_TOKEN"
        ):
            # This error from the notification debugging endpoint should just be directly
            # communicated to the device.
            # TODO: Extend this to use a more general mechanism when we add more such error responses.
            from zerver.lib.push_notifications import InvalidRemotePushDeviceTokenError

            raise InvalidRemotePushDeviceTokenError
        else:
            # But most other errors coming from the push bouncer
            # server are client errors (e.g. never-registered token)
            # and should be handled as such.
            raise JsonableError(msg)
    elif res.status_code != 200:
        # Anything else is unexpected and likely suggests a bug in
        # this version of Zulip, so we throw an exception that will
        # email the server admins.
        raise PushNotificationBouncerError(
            f"Push notification bouncer returned unexpected status code {res.status_code}"
        )

    # If we don't throw an exception, it's a successful bounce!
    return orjson.loads(res.content)


def send_json_to_push_bouncer(
    method: str, endpoint: str, post_data: Mapping[str, object]
) -> Dict[str, object]:
    return send_to_push_bouncer(
        method,
        endpoint,
        orjson.dumps(post_data),
        extra_headers={"Content-type": "application/json"},
    )


REALMAUDITLOG_PUSHED_FIELDS = [
    "id",
    "realm",
    "event_time",
    "backfilled",
    # Note that we don't need to add extra_data_json here because
    # the view remote_server_post_analytics populates extra_data_json
    # from the provided extra_data.
    "extra_data",
    "event_type",
]


def build_analytics_data(
    realm_count_query: Any, installation_count_query: Any, realmauditlog_query: Any
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    # We limit the batch size on the client side to avoid OOM kills timeouts, etc.
    MAX_CLIENT_BATCH_SIZE = 10000
    data = {}
    data["analytics_realmcount"] = [
        model_to_dict(row) for row in realm_count_query.order_by("id")[0:MAX_CLIENT_BATCH_SIZE]
    ]
    data["analytics_installationcount"] = [
        model_to_dict(row)
        for row in installation_count_query.order_by("id")[0:MAX_CLIENT_BATCH_SIZE]
    ]
    data["zerver_realmauditlog"] = [
        model_to_dict(row, fields=REALMAUDITLOG_PUSHED_FIELDS)
        for row in realmauditlog_query.order_by("id")[0:MAX_CLIENT_BATCH_SIZE]
    ]

    floatify_datetime_fields(data, "analytics_realmcount")
    floatify_datetime_fields(data, "analytics_installationcount")
    floatify_datetime_fields(data, "zerver_realmauditlog")
    return (
        data["analytics_realmcount"],
        data["analytics_installationcount"],
        data["zerver_realmauditlog"],
    )


def get_realms_info_for_push_bouncer() -> List[RealmDataForAnalytics]:
    realms = Realm.objects.order_by("id")
    realm_info_list = [
        RealmDataForAnalytics(
            id=realm.id,
            uuid=str(realm.uuid),
            uuid_owner_secret=realm.uuid_owner_secret,
            host=realm.host,
            url=realm.uri,
            deactivated=realm.deactivated,
            date_created=realm.date_created.timestamp(),
        )
        for realm in realms
    ]

    return realm_info_list


def send_analytics_to_push_bouncer() -> None:
    logger = logging.getLogger("zulip.analytics")
    # first, check what's latest
    try:
        result = send_to_push_bouncer("GET", "server/analytics/status", {})
    except PushNotificationBouncerRetryLaterError as e:
        logger.warning(e.msg, exc_info=True)
        return

    last_acked_realm_count_id = result["last_realm_count_id"]
    last_acked_installation_count_id = result["last_installation_count_id"]
    last_acked_realmauditlog_id = result["last_realmauditlog_id"]

    # Gather only entries with IDs greater than the last ID received by the push bouncer.
    # We don't re-send old data that's already been submitted.
    (realm_count_data, installation_count_data, realmauditlog_data) = build_analytics_data(
        realm_count_query=RealmCount.objects.filter(id__gt=last_acked_realm_count_id),
        installation_count_query=InstallationCount.objects.filter(
            id__gt=last_acked_installation_count_id
        ),
        realmauditlog_query=RealmAuditLog.objects.filter(
            event_type__in=RealmAuditLog.SYNCED_BILLING_EVENTS, id__gt=last_acked_realmauditlog_id
        ),
    )

    record_count = len(realm_count_data) + len(installation_count_data) + len(realmauditlog_data)
    if record_count == 0:
        logger.info("No new records to report.")
        return

    request = {
        "realm_counts": orjson.dumps(realm_count_data).decode(),
        "installation_counts": orjson.dumps(installation_count_data).decode(),
        "realmauditlog_rows": orjson.dumps(realmauditlog_data).decode(),
        "realms": orjson.dumps(
            [dict(realm_data) for realm_data in get_realms_info_for_push_bouncer()]
        ).decode(),
        "version": orjson.dumps(ZULIP_VERSION).decode(),
    }

    try:
        send_to_push_bouncer("POST", "server/analytics", request)
    except JsonableError as e:
        logger.warning(e.msg)
    logger.info("Reported %d records", record_count)


def send_realms_only_to_push_bouncer() -> None:
    request = {
        "realm_counts": "[]",
        "installation_counts": "[]",
        "realms": orjson.dumps(
            [dict(realm_data) for realm_data in get_realms_info_for_push_bouncer()]
        ).decode(),
        "version": orjson.dumps(ZULIP_VERSION).decode(),
    }

    # We don't catch JsonableError here, because we want it to propagate further
    # to either explicitly, loudly fail or be error-handled by the caller.
    send_to_push_bouncer("POST", "server/analytics", request)
