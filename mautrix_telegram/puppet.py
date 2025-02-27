# mautrix-telegram - A Matrix-Telegram puppeting bridge
# Copyright (C) 2021 Tulir Asokan
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
from __future__ import annotations

from typing import TYPE_CHECKING, AsyncGenerator, AsyncIterable, Awaitable, cast
from difflib import SequenceMatcher
import unicodedata

from telethon.tl.types import (
    InputPeerPhotoFileLocation,
    PeerUser,
    TypeInputPeer,
    TypeInputUser,
    UpdateUserName,
    User,
    UserProfilePhoto,
    UserProfilePhotoEmpty,
)
from yarl import URL

from mautrix.appservice import IntentAPI
from mautrix.bridge import BasePuppet, async_getter_lock
from mautrix.errors import MatrixError
from mautrix.types import ContentURI, RoomID, SyncToken, UserID
from mautrix.util.simple_template import SimpleTemplate

from . import abstract_user as au, portal as p, util
from .config import Config
from .db import Puppet as DBPuppet
from .types import TelegramID

if TYPE_CHECKING:
    from .__main__ import TelegramBridge


class Puppet(DBPuppet, BasePuppet):
    config: Config
    hs_domain: str
    mxid_template: SimpleTemplate[TelegramID]
    displayname_template: SimpleTemplate[str]

    by_tgid: dict[TelegramID, Puppet] = {}
    by_custom_mxid: dict[UserID, Puppet] = {}

    def __init__(
        self,
        id: TelegramID,
        is_registered: bool = False,
        displayname: str | None = None,
        displayname_source: TelegramID | None = None,
        displayname_contact: bool = True,
        displayname_quality: int = 0,
        disable_updates: bool = False,
        username: str | None = None,
        photo_id: str | None = None,
        is_bot: bool = False,
        custom_mxid: UserID | None = None,
        access_token: str | None = None,
        next_batch: SyncToken | None = None,
        base_url: str | None = None,
    ) -> None:
        super().__init__(
            id=id,
            is_registered=is_registered,
            displayname=displayname,
            displayname_source=displayname_source,
            displayname_contact=displayname_contact,
            displayname_quality=displayname_quality,
            disable_updates=disable_updates,
            username=username,
            photo_id=photo_id,
            is_bot=is_bot,
            custom_mxid=custom_mxid,
            access_token=access_token,
            next_batch=next_batch,
            base_url=base_url,
        )

        self.default_mxid = self.get_mxid_from_id(self.id)
        self.default_mxid_intent = self.az.intent.user(self.default_mxid)
        self.intent = self._fresh_intent()

        self.by_tgid[id] = self
        if self.custom_mxid:
            self.by_custom_mxid[self.custom_mxid] = self

        self.log = self.log.getChild(str(self.id))

    @property
    def tgid(self) -> TelegramID:
        return self.id

    @property
    def tg_username(self) -> str | None:
        return self.username

    @property
    def peer(self) -> PeerUser:
        return PeerUser(user_id=self.tgid)

    @property
    def plain_displayname(self) -> str:
        return self.displayname_template.parse(self.displayname) or self.displayname

    def get_input_entity(self, user: au.AbstractUser) -> Awaitable[TypeInputPeer | TypeInputUser]:
        return user.client.get_input_entity(self.peer)

    def intent_for(self, portal: p.Portal) -> IntentAPI:
        if portal.tgid == self.tgid:
            return self.default_mxid_intent
        return self.intent

    @classmethod
    def init_cls(cls, bridge: "TelegramBridge") -> AsyncIterable[Awaitable[None]]:
        cls.config = bridge.config
        cls.loop = bridge.loop
        cls.mx = bridge.matrix
        cls.az = bridge.az
        cls.hs_domain = cls.config["homeserver.domain"]
        mxid_tpl = SimpleTemplate(
            cls.config["bridge.username_template"],
            "userid",
            prefix="@",
            suffix=f":{Puppet.hs_domain}",
            type=int,
        )
        cls.mxid_template = cast(SimpleTemplate[TelegramID], mxid_tpl)
        cls.displayname_template = SimpleTemplate(
            cls.config["bridge.displayname_template"], "displayname"
        )
        cls.sync_with_custom_puppets = cls.config["bridge.sync_with_custom_puppets"]
        cls.homeserver_url_map = {
            server: URL(url)
            for server, url in cls.config["bridge.double_puppet_server_map"].items()
        }
        cls.allow_discover_url = cls.config["bridge.double_puppet_allow_discovery"]
        cls.login_shared_secret_map = {
            server: secret.encode("utf-8")
            for server, secret in cls.config["bridge.login_shared_secret_map"].items()
        }
        cls.login_device_name = "Telegram Bridge"

        return (puppet.try_start() async for puppet in cls.all_with_custom_mxid())

    # region Info updating

    def similarity(self, query: str) -> int:
        username_similarity = (
            SequenceMatcher(None, self.username, query).ratio() if self.username else 0
        )
        displayname_similarity = (
            SequenceMatcher(None, self.plain_displayname, query).ratio() if self.displayname else 0
        )
        similarity = max(username_similarity, displayname_similarity)
        return int(round(similarity * 100))

    @staticmethod
    def _filter_name(name: str) -> str:
        if not name:
            return ""
        whitespace = (
            "\t\n\r\v\f \u00a0\u034f\u180e\u2063\u202f\u205f\u2800\u3000\u3164\ufeff\u2000\u2001"
            "\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u200b\u200c\u200d\u200e\u200f"
            "\ufe0f"
        )
        allowed_other_format = ("\u200d", "\u200c")
        name = "".join(
            c
            for c in name.strip(whitespace)
            if unicodedata.category(c) != "Cf" or c in allowed_other_format
        )
        return name

    @classmethod
    def get_displayname(cls, info: User, enable_format: bool = True) -> tuple[str, int]:
        fn = cls._filter_name(info.first_name)
        ln = cls._filter_name(info.last_name)
        data = {
            "phone number": info.phone if hasattr(info, "phone") else None,
            "username": info.username,
            "full name": " ".join([fn, ln]).strip(),
            "full name reversed": " ".join([ln, fn]).strip(),
            "first name": fn,
            "last name": ln,
        }
        preferences = cls.config["bridge.displayname_preference"]
        name = None
        quality = 99
        for preference in preferences:
            name = data[preference]
            if name:
                break
            quality -= 1

        if isinstance(info, User) and info.deleted:
            name = f"Deleted account {info.id}"
            quality = 99
        elif not name:
            name = str(info.id)
            quality = 0

        return (cls.displayname_template.format_full(name) if enable_format else name), quality

    async def try_update_info(self, source: au.AbstractUser, info: User) -> None:
        try:
            await self.update_info(source, info)
        except Exception:
            source.log.exception(f"Failed to update info of {self.tgid}")

    async def update_info(self, source: au.AbstractUser, info: User) -> None:
        changed = False
        if self.username != info.username:
            self.username = info.username
            changed = True

        if not self.disable_updates:
            try:
                changed = await self.update_displayname(source, info) or changed
                changed = await self.update_avatar(source, info.photo) or changed
            except Exception:
                self.log.exception(f"Failed to update info from source {source.tgid}")

        self.is_bot = info.bot

        if changed:
            await self.save()

    async def update_displayname(
        self, source: au.AbstractUser, info: User | UpdateUserName
    ) -> bool:
        if self.disable_updates:
            return False
        if source.is_relaybot or source.is_bot:
            allow_because = "user is bot"
        elif self.displayname_source == source.tgid:
            allow_because = "user is the primary source"
        elif not isinstance(info, UpdateUserName) and not info.contact:
            allow_because = "user is not a contact"
        elif not self.displayname_source:
            allow_because = "no primary source set"
        elif not self.displayname:
            allow_because = "user has no name"
        else:
            return False

        if isinstance(info, UpdateUserName):
            info = await source.client.get_entity(PeerUser(self.tgid))
        if not info.contact:
            self.displayname_contact = False
        elif not self.displayname_contact:
            if not self.displayname:
                self.displayname_contact = True
            else:
                return False

        displayname, quality = self.get_displayname(info)
        if displayname != self.displayname and quality >= self.displayname_quality:
            allow_because = f"{allow_because} and quality {quality} >= {self.displayname_quality}"
            self.log.debug(
                f"Updating displayname of {self.id} (src: {source.tgid}, allowed "
                f"because {allow_because}) from {self.displayname} to {displayname}"
            )
            self.log.trace("Displayname source data: %s", info)
            self.displayname = displayname
            self.displayname_source = source.tgid
            self.displayname_quality = quality
            try:
                await self.default_mxid_intent.set_displayname(
                    displayname[: self.config["bridge.displayname_max_length"]]
                )
            except MatrixError:
                self.log.exception("Failed to set displayname")
                self.displayname = ""
                self.displayname_source = None
                self.displayname_quality = 0
            return True
        elif source.is_relaybot or self.displayname_source is None:
            self.displayname_source = source.tgid
            return True
        return False

    async def update_avatar(
        self, source: au.AbstractUser, photo: UserProfilePhoto | UserProfilePhotoEmpty
    ) -> bool:
        if self.disable_updates:
            return False

        if photo is None or isinstance(photo, UserProfilePhotoEmpty):
            photo_id = ""
        elif isinstance(photo, UserProfilePhoto):
            photo_id = str(photo.photo_id)
        else:
            self.log.warning(f"Unknown user profile photo type: {type(photo)}")
            return False
        if not photo_id and not self.config["bridge.allow_avatar_remove"]:
            return False
        if self.photo_id != photo_id:
            if not photo_id:
                self.photo_id = ""
                try:
                    await self.default_mxid_intent.set_avatar_url(ContentURI(""))
                except MatrixError:
                    self.log.exception("Failed to set avatar")
                    self.photo_id = ""
                return True

            loc = InputPeerPhotoFileLocation(
                peer=await self.get_input_entity(source), photo_id=photo.photo_id, big=True
            )
            file = await util.transfer_file_to_matrix(source.client, self.default_mxid_intent, loc)
            if file:
                self.photo_id = photo_id
                try:
                    await self.default_mxid_intent.set_avatar_url(file.mxc)
                except MatrixError:
                    self.log.exception("Failed to set avatar")
                    self.photo_id = ""
                return True
        return False

    async def default_puppet_should_leave_room(self, room_id: RoomID) -> bool:
        portal: p.Portal = await p.Portal.get_by_mxid(room_id)
        return portal and not portal.backfill_lock.locked and portal.peer_type != "user"

    # endregion
    # region Getters

    def _add_to_cache(self) -> None:
        self.by_tgid[self.id] = self
        if self.custom_mxid:
            self.by_custom_mxid[self.custom_mxid] = self

    @classmethod
    @async_getter_lock
    async def get_by_tgid(cls, tgid: TelegramID, *, create: bool = True) -> Puppet | None:
        if tgid is None:
            return None

        try:
            return cls.by_tgid[tgid]
        except KeyError:
            pass

        puppet = cast(cls, await super().get_by_tgid(tgid))
        if puppet:
            puppet._add_to_cache()
            return puppet

        if create:
            puppet = cls(tgid)
            await puppet.insert()
            puppet._add_to_cache()
            return puppet

        return None

    @classmethod
    def get_by_mxid(cls, mxid: UserID, create: bool = True) -> Awaitable[Puppet | None]:
        return cls.get_by_tgid(cls.get_id_from_mxid(mxid), create=create)

    @classmethod
    @async_getter_lock
    async def get_by_custom_mxid(cls, mxid: UserID) -> Puppet | None:
        try:
            return cls.by_custom_mxid[mxid]
        except KeyError:
            pass

        puppet = cast(cls, await super().get_by_custom_mxid(mxid))
        if puppet:
            puppet._add_to_cache()
            return puppet

        return None

    @classmethod
    async def all_with_custom_mxid(cls) -> AsyncGenerator[Puppet, None]:
        puppets = await super().all_with_custom_mxid()
        puppet: cls
        for puppet in puppets:
            try:
                yield cls.by_tgid[puppet.tgid]
            except KeyError:
                puppet._add_to_cache()
                yield puppet

    @classmethod
    def get_id_from_mxid(cls, mxid: UserID) -> TelegramID | None:
        return cls.mxid_template.parse(mxid)

    @classmethod
    def get_mxid_from_id(cls, tgid: TelegramID) -> UserID:
        return UserID(cls.mxid_template.format_full(tgid))

    @classmethod
    async def find_by_username(cls, username: str) -> Puppet | None:
        if not username:
            return None

        username = username.lower()

        for _, puppet in cls.by_tgid.items():
            if puppet.username and puppet.username.lower() == username:
                return puppet

        puppet = cast(cls, await super().find_by_username(username))
        if puppet:
            try:
                return cls.by_tgid[puppet.tgid]
            except KeyError:
                puppet._add_to_cache()
                return puppet

        return None

    @classmethod
    async def find_by_displayname(cls, displayname: str) -> Puppet | None:
        if not displayname:
            return None

        for _, puppet in cls.by_tgid.items():
            if puppet.displayname and puppet.displayname == displayname:
                return puppet

        puppet = cast(cls, await super().find_by_displayname(displayname))
        if puppet:
            try:
                return cls.by_tgid[puppet.tgid]
            except KeyError:
                puppet._add_to_cache()
                return puppet

        return None

    # endregion
