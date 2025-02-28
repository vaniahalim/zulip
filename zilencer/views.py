import datetime
import logging
from collections import Counter
from typing import Any, Dict, List, Optional, Type, TypeVar, Union
from uuid import UUID

import orjson
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator, validate_email
from django.db import IntegrityError, transaction
from django.db.models import Model
from django.http import HttpRequest, HttpResponse
from django.utils.crypto import constant_time_compare
from django.utils.timezone import now as timezone_now
from django.utils.translation import gettext as _
from django.utils.translation import gettext as err_
from django.views.decorators.csrf import csrf_exempt
from pydantic import BaseModel, ConfigDict, Json

from analytics.lib.counts import (
    BOUNCER_ONLY_REMOTE_COUNT_STAT_PROPERTIES,
    COUNT_STATS,
    REMOTE_INSTALLATION_COUNT_STATS,
    do_increment_logging_stat,
)
from corporate.lib.stripe import do_deactivate_remote_server
from zerver.decorator import require_post
from zerver.lib.exceptions import JsonableError
from zerver.lib.push_notifications import (
    InvalidRemotePushDeviceTokenError,
    UserPushIdentityCompat,
    send_android_push_notification,
    send_apple_push_notification,
    send_test_push_notification_directly_to_devices,
)
from zerver.lib.remote_server import RealmDataForAnalytics
from zerver.lib.request import REQ, has_request_variables
from zerver.lib.response import json_success
from zerver.lib.timestamp import timestamp_to_datetime
from zerver.lib.typed_endpoint import JsonBodyPayload, typed_endpoint
from zerver.lib.validator import check_capped_string, check_int, check_string_fixed_length
from zerver.views.push_notifications import check_app_id, validate_token
from zilencer.auth import InvalidZulipServerKeyError
from zilencer.models import (
    RemoteInstallationCount,
    RemotePushDeviceToken,
    RemoteRealm,
    RemoteRealmAuditLog,
    RemoteRealmCount,
    RemoteZulipServer,
    RemoteZulipServerAuditLog,
)

logger = logging.getLogger(__name__)


def validate_uuid(uuid: str) -> None:
    try:
        uuid_object = UUID(uuid, version=4)
        # The UUID initialization under some circumstances will modify the uuid
        # string to create a valid UUIDv4, instead of raising a ValueError.
        # The submitted uuid needing to be modified means it's invalid, so
        # we need to check for that condition.
        if str(uuid_object) != uuid:
            raise ValidationError(err_("Invalid UUID"))
    except ValueError:
        raise ValidationError(err_("Invalid UUID"))


def validate_bouncer_token_request(token: str, kind: int) -> None:
    if kind not in [RemotePushDeviceToken.APNS, RemotePushDeviceToken.GCM]:
        raise JsonableError(err_("Invalid token type"))
    validate_token(token, kind)


@csrf_exempt
@require_post
@has_request_variables
def deactivate_remote_server(
    request: HttpRequest,
    remote_server: RemoteZulipServer,
) -> HttpResponse:
    do_deactivate_remote_server(remote_server)
    return json_success(request)


@csrf_exempt
@require_post
@has_request_variables
def register_remote_server(
    request: HttpRequest,
    zulip_org_id: str = REQ(str_validator=check_string_fixed_length(RemoteZulipServer.UUID_LENGTH)),
    zulip_org_key: str = REQ(
        str_validator=check_string_fixed_length(RemoteZulipServer.API_KEY_LENGTH)
    ),
    hostname: str = REQ(str_validator=check_capped_string(RemoteZulipServer.HOSTNAME_MAX_LENGTH)),
    contact_email: str = REQ(),
    new_org_key: Optional[str] = REQ(
        str_validator=check_string_fixed_length(RemoteZulipServer.API_KEY_LENGTH), default=None
    ),
) -> HttpResponse:
    # REQ validated the the field lengths, but we still need to
    # validate the format of these fields.
    try:
        # TODO: Ideally we'd not abuse the URL validator this way
        url_validator = URLValidator()
        url_validator("http://" + hostname)
    except ValidationError:
        raise JsonableError(_("{hostname} is not a valid hostname").format(hostname=hostname))

    try:
        validate_email(contact_email)
    except ValidationError as e:
        raise JsonableError(e.message)

    try:
        validate_uuid(zulip_org_id)
    except ValidationError as e:
        raise JsonableError(e.message)

    with transaction.atomic():
        remote_server, created = RemoteZulipServer.objects.get_or_create(
            uuid=zulip_org_id,
            defaults={
                "hostname": hostname,
                "contact_email": contact_email,
                "api_key": zulip_org_key,
            },
        )
        if created:
            RemoteZulipServerAuditLog.objects.create(
                event_type=RemoteZulipServerAuditLog.REMOTE_SERVER_CREATED,
                server=remote_server,
                event_time=remote_server.last_updated,
            )
        else:
            if not constant_time_compare(remote_server.api_key, zulip_org_key):
                raise InvalidZulipServerKeyError(zulip_org_id)
            else:
                remote_server.hostname = hostname
                remote_server.contact_email = contact_email
                if new_org_key is not None:
                    remote_server.api_key = new_org_key
                remote_server.save()

    return json_success(request, data={"created": created})


@has_request_variables
def register_remote_push_device(
    request: HttpRequest,
    server: RemoteZulipServer,
    user_id: Optional[int] = REQ(json_validator=check_int, default=None),
    user_uuid: Optional[str] = REQ(default=None),
    token: str = REQ(),
    token_kind: int = REQ(json_validator=check_int),
    ios_app_id: Optional[str] = REQ(str_validator=check_app_id, default=None),
) -> HttpResponse:
    validate_bouncer_token_request(token, token_kind)
    if token_kind == RemotePushDeviceToken.APNS and ios_app_id is None:
        raise JsonableError(_("Missing ios_app_id"))

    if user_id is None and user_uuid is None:
        raise JsonableError(_("Missing user_id or user_uuid"))
    if user_id is not None and user_uuid is not None:
        kwargs: Dict[str, object] = {"user_uuid": user_uuid, "user_id": None}
        # Delete pre-existing user_id registration for this user+device to avoid
        # duplication. Further down, uuid registration will be created.
        RemotePushDeviceToken.objects.filter(
            server=server, token=token, kind=token_kind, user_id=user_id
        ).delete()
    else:
        # One of these is None, so these kwargs will lead to a proper registration
        # of either user_id or user_uuid type
        kwargs = {"user_id": user_id, "user_uuid": user_uuid}
    try:
        with transaction.atomic():
            RemotePushDeviceToken.objects.create(
                server=server,
                kind=token_kind,
                token=token,
                ios_app_id=ios_app_id,
                # last_updated is to be renamed to date_created.
                last_updated=timezone_now(),
                **kwargs,
            )
    except IntegrityError:
        pass

    return json_success(request)


@has_request_variables
def unregister_remote_push_device(
    request: HttpRequest,
    server: RemoteZulipServer,
    token: str = REQ(),
    token_kind: int = REQ(json_validator=check_int),
    user_id: Optional[int] = REQ(json_validator=check_int, default=None),
    user_uuid: Optional[str] = REQ(default=None),
) -> HttpResponse:
    validate_bouncer_token_request(token, token_kind)
    user_identity = UserPushIdentityCompat(user_id=user_id, user_uuid=user_uuid)

    (num_deleted, ignored) = RemotePushDeviceToken.objects.filter(
        user_identity.filter_q(), token=token, kind=token_kind, server=server
    ).delete()
    if num_deleted == 0:
        raise JsonableError(err_("Token does not exist"))

    return json_success(request)


@has_request_variables
def unregister_all_remote_push_devices(
    request: HttpRequest,
    server: RemoteZulipServer,
    user_id: Optional[int] = REQ(json_validator=check_int, default=None),
    user_uuid: Optional[str] = REQ(default=None),
) -> HttpResponse:
    user_identity = UserPushIdentityCompat(user_id=user_id, user_uuid=user_uuid)

    RemotePushDeviceToken.objects.filter(user_identity.filter_q(), server=server).delete()
    return json_success(request)


def delete_duplicate_registrations(
    registrations: List[RemotePushDeviceToken], server_id: int, user_id: int, user_uuid: str
) -> List[RemotePushDeviceToken]:
    """
    When migrating to support registration by UUID, we introduced a bug where duplicate
    registrations for the same device+user could be created - one by user_id and one by
    user_uuid. Given no good way of detecting these duplicates at database level, we need to
    take advantage of the fact that when a remote server sends a push notification request
    to us, it sends both user_id and user_uuid of the user.
    See https://github.com/zulip/zulip/issues/24969 for reference.

    This function, knowing the user_id and user_uuid of the user, can detect duplicates
    and delete the legacy user_id registration if appropriate.

    Return the list of registrations with the user_id-based duplicates removed.
    """

    # All registrations passed here should be of the same kind (apple vs android).
    assert len({registration.kind for registration in registrations}) == 1
    kind = registrations[0].kind

    tokens_counter = Counter(device.token for device in registrations)

    tokens_to_deduplicate = []
    for key in tokens_counter:
        if tokens_counter[key] <= 1:
            continue
        if tokens_counter[key] > 2:
            raise AssertionError(
                f"More than two registrations for token {key} for user id:{user_id} uuid:{user_uuid}, shouldn't be possible"
            )
        assert tokens_counter[key] == 2
        tokens_to_deduplicate.append(key)

    if not tokens_to_deduplicate:
        return registrations

    logger.info(
        "Deduplicating push registrations for server id:%s user id:%s uuid:%s and tokens:%s",
        server_id,
        user_id,
        user_uuid,
        sorted(tokens_to_deduplicate),
    )
    RemotePushDeviceToken.objects.filter(
        token__in=tokens_to_deduplicate, kind=kind, server_id=server_id, user_id=user_id
    ).delete()

    deduplicated_registrations_to_return = []
    for registration in registrations:
        if registration.token in tokens_to_deduplicate and registration.user_id is not None:
            # user_id registrations are the ones we deleted
            continue
        deduplicated_registrations_to_return.append(registration)

    return deduplicated_registrations_to_return


class TestNotificationPayload(BaseModel):
    token: str
    token_kind: int
    user_id: int
    user_uuid: str
    base_payload: Dict[str, Any]

    model_config = ConfigDict(extra="forbid")


@typed_endpoint
def remote_server_send_test_notification(
    request: HttpRequest,
    server: RemoteZulipServer,
    *,
    payload: JsonBodyPayload[TestNotificationPayload],
) -> HttpResponse:
    token = payload.token
    token_kind = payload.token_kind

    user_id = payload.user_id
    user_uuid = payload.user_uuid

    # The remote server only sends the base payload with basic user and server info,
    # and the actual format of the test notification is defined on the bouncer, as that
    # gives us the flexibility to modify it freely, without relying on other servers
    # upgrading.
    base_payload = payload.base_payload

    # This is a new endpoint, so it can assume it will only be used by newer
    # servers that will send user both UUID and ID.
    user_identity = UserPushIdentityCompat(user_id=user_id, user_uuid=user_uuid)

    try:
        device = RemotePushDeviceToken.objects.get(
            user_identity.filter_q(), token=token, kind=token_kind, server=server
        )
    except RemotePushDeviceToken.DoesNotExist:
        raise InvalidRemotePushDeviceTokenError

    send_test_push_notification_directly_to_devices(
        user_identity, [device], base_payload, remote=server
    )
    return json_success(request)


@has_request_variables
def remote_server_notify_push(
    request: HttpRequest,
    server: RemoteZulipServer,
    payload: Dict[str, Any] = REQ(argument_type="body"),
) -> HttpResponse:
    user_id = payload.get("user_id")
    user_uuid = payload.get("user_uuid")
    user_identity = UserPushIdentityCompat(user_id, user_uuid)

    gcm_payload = payload["gcm_payload"]
    apns_payload = payload["apns_payload"]
    gcm_options = payload.get("gcm_options", {})

    realm_uuid = payload.get("realm_uuid")
    remote_realm = None
    if realm_uuid is not None:
        try:
            remote_realm = RemoteRealm.objects.get(uuid=realm_uuid, server=server)
        except RemoteRealm.DoesNotExist:
            # We don't yet have a RemoteRealm for this realm. E.g. the server hasn't yet
            # submitted analytics data since the realm's creation.
            remote_realm = None

    android_devices = list(
        RemotePushDeviceToken.objects.filter(
            user_identity.filter_q(),
            kind=RemotePushDeviceToken.GCM,
            server=server,
        )
    )
    if android_devices and user_id is not None and user_uuid is not None:
        android_devices = delete_duplicate_registrations(
            android_devices, server.id, user_id, user_uuid
        )

    apple_devices = list(
        RemotePushDeviceToken.objects.filter(
            user_identity.filter_q(),
            kind=RemotePushDeviceToken.APNS,
            server=server,
        )
    )
    if apple_devices and user_id is not None and user_uuid is not None:
        apple_devices = delete_duplicate_registrations(apple_devices, server.id, user_id, user_uuid)

    remote_queue_latency: Optional[str] = None
    sent_time: Optional[Union[float, int]] = gcm_payload.get(
        # TODO/compatibility: This could be a lot simpler if not for pre-5.0 Zulip servers
        # that had an older format. Future implementation:
        #     "time", apns_payload["custom"]["zulip"].get("time")
        "time",
        apns_payload.get("custom", {}).get("zulip", {}).get("time"),
    )
    if sent_time is not None:
        if isinstance(sent_time, int):
            # The 'time' field only used to have whole-integer
            # granularity, so if so we only report with
            # whole-second granularity
            remote_queue_latency = str(int(timezone_now().timestamp()) - sent_time)
        else:
            remote_queue_latency = f"{timezone_now().timestamp() - sent_time:.3f}"
        logger.info(
            "Remote queuing latency for %s:%s is %s seconds",
            server.uuid,
            user_identity,
            remote_queue_latency,
        )

    logger.info(
        "Sending mobile push notifications for remote user %s:%s: %s via FCM devices, %s via APNs devices",
        server.uuid,
        user_identity,
        len(android_devices),
        len(apple_devices),
    )
    do_increment_logging_stat(
        server,
        REMOTE_INSTALLATION_COUNT_STATS["mobile_pushes_received::day"],
        None,
        timezone_now(),
        increment=len(android_devices) + len(apple_devices),
    )
    if remote_realm is not None:
        do_increment_logging_stat(
            remote_realm,
            COUNT_STATS["mobile_pushes_received::day"],
            None,
            timezone_now(),
            increment=len(android_devices) + len(apple_devices),
        )

    # Truncate incoming pushes to 200, due to APNs maximum message
    # sizes; see handle_remove_push_notification for the version of
    # this for notifications generated natively on the server.  We
    # apply this to remote-server pushes in case they predate that
    # commit.
    def truncate_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
        MAX_MESSAGE_IDS = 200
        if payload and payload.get("event") == "remove" and payload.get("zulip_message_ids"):
            ids = [int(id) for id in payload["zulip_message_ids"].split(",")]
            truncated_ids = sorted(ids)[-MAX_MESSAGE_IDS:]
            payload["zulip_message_ids"] = ",".join(str(id) for id in truncated_ids)
        return payload

    # The full request must complete within 30s, the timeout set by
    # Zulip remote hosts for push notification requests (see
    # PushBouncerSession).  The timeouts in the FCM and APNS codepaths
    # must be set accordingly; see send_android_push_notification and
    # send_apple_push_notification.

    gcm_payload = truncate_payload(gcm_payload)
    android_successfully_delivered = send_android_push_notification(
        user_identity, android_devices, gcm_payload, gcm_options, remote=server
    )

    if isinstance(apns_payload.get("custom"), dict) and isinstance(
        apns_payload["custom"].get("zulip"), dict
    ):
        apns_payload["custom"]["zulip"] = truncate_payload(apns_payload["custom"]["zulip"])
    apple_successfully_delivered = send_apple_push_notification(
        user_identity, apple_devices, apns_payload, remote=server
    )

    do_increment_logging_stat(
        server,
        REMOTE_INSTALLATION_COUNT_STATS["mobile_pushes_forwarded::day"],
        None,
        timezone_now(),
        increment=android_successfully_delivered + apple_successfully_delivered,
    )
    if remote_realm is not None:
        do_increment_logging_stat(
            remote_realm,
            COUNT_STATS["mobile_pushes_forwarded::day"],
            None,
            timezone_now(),
            increment=android_successfully_delivered + apple_successfully_delivered,
        )

    return json_success(
        request,
        data={
            "total_android_devices": len(android_devices),
            "total_apple_devices": len(apple_devices),
        },
    )


def validate_incoming_table_data(
    server: RemoteZulipServer, model: Any, rows: List[Dict[str, Any]], is_count_stat: bool = False
) -> None:
    last_id = get_last_id_from_server(server, model)
    for row in rows:
        if is_count_stat and (
            row["property"] not in COUNT_STATS
            or row["property"] in BOUNCER_ONLY_REMOTE_COUNT_STAT_PROPERTIES
        ):
            raise JsonableError(_("Invalid property {property}").format(property=row["property"]))
        if row.get("id") is None:
            # This shouldn't be possible, as submitting data like this should be
            # prevented by our param validators.
            raise AssertionError(f"Missing id field in row {row}")
        if row["id"] <= last_id:
            raise JsonableError(_("Data is out of order."))
        last_id = row["id"]


ModelT = TypeVar("ModelT", bound=Model)


def batch_create_table_data(
    server: RemoteZulipServer,
    model: Type[ModelT],
    row_objects: List[ModelT],
) -> None:
    # We ignore previously-existing data, in case it was truncated and
    # re-created on the remote server.  `ignore_concflicts=True`
    # cannot return the ids, or count thereof, of the new inserts,
    # (see https://code.djangoproject.com/ticket/0138) so we rely on
    # having a lock to accurately count them before and after.  This
    # query is also well-indexed.
    before_count = model._default_manager.filter(server=server).count()
    model._default_manager.bulk_create(row_objects, batch_size=1000, ignore_conflicts=True)
    after_count = model._default_manager.filter(server=server).count()
    inserted_count = after_count - before_count
    if inserted_count < len(row_objects):
        logging.warning(
            "Dropped %d duplicated rows while saving %d rows of %s for server %s/%s",
            len(row_objects) - inserted_count,
            len(row_objects),
            model._meta.db_table,
            server.hostname,
            server.uuid,
        )


def update_remote_realm_data_for_server(
    server: RemoteZulipServer, server_realms_info: List[Dict[str, Any]]
) -> None:
    uuids = [realm["uuid"] for realm in server_realms_info]
    already_registered_remote_realms = RemoteRealm.objects.filter(uuid__in=uuids, server=server)
    already_registered_uuids = {
        str(remote_realm.uuid) for remote_realm in already_registered_remote_realms
    }

    new_remote_realms = [
        RemoteRealm(
            server=server,
            uuid=realm["uuid"],
            uuid_owner_secret=realm["uuid_owner_secret"],
            host=realm["host"],
            realm_deactivated=realm["deactivated"],
            realm_date_created=timestamp_to_datetime(realm["date_created"]),
        )
        for realm in server_realms_info
        if realm["uuid"] not in already_registered_uuids
    ]

    try:
        RemoteRealm.objects.bulk_create(new_remote_realms)
    except IntegrityError:
        raise JsonableError(_("Duplicate registration detected."))

    uuid_to_realm_dict = {str(realm["uuid"]): realm for realm in server_realms_info}
    remote_realms_to_update = []
    remote_realm_audit_logs = []
    now = timezone_now()

    # Update RemoteRealm entries, for which the corresponding realm's info has changed
    # (for the attributes that make sense to sync like this).
    for remote_realm in already_registered_remote_realms:
        modified = False
        realm = uuid_to_realm_dict[str(remote_realm.uuid)]
        for remote_realm_attr, realm_dict_key in [
            ("host", "host"),
            ("realm_deactivated", "deactivated"),
        ]:
            old_value = getattr(remote_realm, remote_realm_attr)
            new_value = realm[realm_dict_key]
            if old_value == new_value:
                continue

            setattr(remote_realm, remote_realm_attr, new_value)
            remote_realm_audit_logs.append(
                RemoteRealmAuditLog(
                    server=server,
                    remote_id=None,
                    remote_realm=remote_realm,
                    realm_id=realm["id"],
                    event_type=RemoteRealmAuditLog.REMOTE_REALM_VALUE_UPDATED,
                    event_time=now,
                    extra_data={
                        "attr_name": remote_realm_attr,
                        "old_value": old_value,
                        "new_value": new_value,
                    },
                )
            )
            modified = True

        if modified:
            remote_realms_to_update.append(remote_realm)

    RemoteRealm.objects.bulk_update(remote_realms_to_update, ["host", "realm_deactivated"])
    RemoteRealmAuditLog.objects.bulk_create(remote_realm_audit_logs)


class RealmAuditLogDataForAnalytics(BaseModel):
    id: int
    realm: int
    event_time: float
    backfilled: bool
    extra_data: Optional[Union[str, Dict[str, Any]]]
    event_type: int


class RealmCountDataForAnalytics(BaseModel):
    property: str
    realm: int
    id: int
    end_time: float
    subgroup: Optional[str]
    value: int


class InstallationCountDataForAnalytics(BaseModel):
    property: str
    id: int
    end_time: float
    subgroup: Optional[str]
    value: int


@typed_endpoint
@transaction.atomic
def remote_server_post_analytics(
    request: HttpRequest,
    server: RemoteZulipServer,
    *,
    realm_counts: Json[List[RealmCountDataForAnalytics]],
    installation_counts: Json[List[InstallationCountDataForAnalytics]],
    realmauditlog_rows: Optional[Json[List[RealmAuditLogDataForAnalytics]]] = None,
    realms: Optional[Json[List[RealmDataForAnalytics]]] = None,
    version: Optional[Json[str]] = None,
) -> HttpResponse:
    # Lock the server, preventing this from racing with other
    # duplicate submissions of the data
    server = RemoteZulipServer.objects.select_for_update().get(id=server.id)

    if version is not None:
        version = version[0 : RemoteZulipServer.VERSION_MAX_LENGTH]
    if version != server.last_version:
        server.last_version = version
        server.save(update_fields=["last_version"])

    validate_incoming_table_data(
        server, RemoteRealmCount, [dict(count) for count in realm_counts], True
    )
    validate_incoming_table_data(
        server, RemoteInstallationCount, [dict(count) for count in installation_counts], True
    )

    if realmauditlog_rows is not None:
        validate_incoming_table_data(
            server, RemoteRealmAuditLog, [dict(row) for row in realmauditlog_rows]
        )

    if realms is not None:
        update_remote_realm_data_for_server(server, [dict(realm) for realm in realms])

    remote_realm_counts = [
        RemoteRealmCount(
            property=row.property,
            realm_id=row.realm,
            remote_id=row.id,
            server=server,
            end_time=datetime.datetime.fromtimestamp(row.end_time, tz=datetime.timezone.utc),
            subgroup=row.subgroup,
            value=row.value,
        )
        for row in realm_counts
    ]
    batch_create_table_data(server, RemoteRealmCount, remote_realm_counts)

    remote_installation_counts = [
        RemoteInstallationCount(
            property=row.property,
            remote_id=row.id,
            server=server,
            end_time=datetime.datetime.fromtimestamp(row.end_time, tz=datetime.timezone.utc),
            subgroup=row.subgroup,
            value=row.value,
        )
        for row in installation_counts
    ]
    batch_create_table_data(server, RemoteInstallationCount, remote_installation_counts)

    if realmauditlog_rows is not None:
        remote_realm_audit_logs = []
        for row in realmauditlog_rows:
            extra_data = {}
            if isinstance(row.extra_data, str):
                try:
                    extra_data = orjson.loads(row.extra_data)
                except orjson.JSONDecodeError:
                    raise JsonableError(_("Malformed audit log data"))
            elif row.extra_data is not None:
                assert isinstance(row.extra_data, dict)
                extra_data = row.extra_data
            remote_realm_audit_logs.append(
                RemoteRealmAuditLog(
                    realm_id=row.realm,
                    remote_id=row.id,
                    server=server,
                    event_time=datetime.datetime.fromtimestamp(
                        row.event_time, tz=datetime.timezone.utc
                    ),
                    backfilled=row.backfilled,
                    extra_data=extra_data,
                    event_type=row.event_type,
                )
            )
        batch_create_table_data(server, RemoteRealmAuditLog, remote_realm_audit_logs)

    return json_success(request)


def get_last_id_from_server(server: RemoteZulipServer, model: Any) -> int:
    last_count = (
        model.objects.filter(server=server)
        # Rows with remote_id=None are managed by the bouncer service itself,
        # and thus aren't meant for syncing and should be ignored here.
        .exclude(remote_id=None)
        .order_by("remote_id")
        .only("remote_id")
        .last()
    )
    if last_count is not None:
        return last_count.remote_id
    return 0


@has_request_variables
def remote_server_check_analytics(request: HttpRequest, server: RemoteZulipServer) -> HttpResponse:
    result = {
        "last_realm_count_id": get_last_id_from_server(server, RemoteRealmCount),
        "last_installation_count_id": get_last_id_from_server(server, RemoteInstallationCount),
        "last_realmauditlog_id": get_last_id_from_server(server, RemoteRealmAuditLog),
    }
    return json_success(request, data=result)
