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

from typing import TYPE_CHECKING, ClassVar

from asyncpg import Record
from attr import dataclass
from yarl import URL

from mautrix.types import SyncToken, UserID
from mautrix.util.async_db import Database

from ..types import TelegramID

fake_db = Database.create("") if TYPE_CHECKING else None


@dataclass
class Puppet:
    db: ClassVar[Database] = fake_db

    id: TelegramID

    is_registered: bool

    displayname: str | None
    displayname_source: TelegramID | None
    displayname_contact: bool
    displayname_quality: int
    disable_updates: bool
    username: str | None
    photo_id: str | None
    is_bot: bool | None

    custom_mxid: UserID | None
    access_token: str | None
    next_batch: SyncToken | None
    base_url: URL | None

    @classmethod
    def _from_row(cls, row: Record | None) -> Puppet | None:
        if row is None:
            return None
        data = {**row}
        base_url = data.pop("base_url", None)
        return cls(**data, base_url=URL(base_url) if base_url else None)

    columns: ClassVar[str] = (
        "id, is_registered, displayname, displayname_source, displayname_contact, "
        "displayname_quality, disable_updates, username, photo_id, is_bot, "
        "custom_mxid, access_token, next_batch, base_url"
    )

    @classmethod
    async def all_with_custom_mxid(cls) -> list[Puppet]:
        q = f"SELECT {cls.columns} FROM puppet WHERE custom_mxid<>''"
        return [cls._from_row(row) for row in await cls.db.fetch(q)]

    @classmethod
    async def get_by_tgid(cls, tgid: TelegramID) -> Puppet | None:
        q = f"SELECT {cls.columns} FROM puppet WHERE id=$1"
        return cls._from_row(await cls.db.fetchrow(q, tgid))

    @classmethod
    async def get_by_custom_mxid(cls, mxid: UserID) -> Puppet | None:
        q = f"SELECT {cls.columns} FROM puppet WHERE custom_mxid=$1"
        return cls._from_row(await cls.db.fetchrow(q, mxid))

    @classmethod
    async def find_by_username(cls, username: str) -> Puppet | None:
        q = f"SELECT {cls.columns} FROM puppet WHERE lower(username)=$1"
        return cls._from_row(await cls.db.fetchrow(q, username.lower()))

    @classmethod
    async def find_by_displayname(cls, displayname: str) -> Puppet | None:
        q = f"SELECT {cls.columns} FROM puppet WHERE displayname=$1"
        return cls._from_row(await cls.db.fetchrow(q, displayname))

    @property
    def _values(self):
        return (
            self.id,
            self.is_registered,
            self.displayname,
            self.displayname_source,
            self.displayname_contact,
            self.displayname_quality,
            self.disable_updates,
            self.username,
            self.photo_id,
            self.is_bot,
            self.custom_mxid,
            self.access_token,
            self.next_batch,
            str(self.base_url) if self.base_url else None,
        )

    async def save(self) -> None:
        q = (
            "UPDATE puppet "
            "SET is_registered=$2, displayname=$3, displayname_source=$4, displayname_contact=$5,"
            "    displayname_quality=$6, disable_updates=$7, username=$8, photo_id=$9, is_bot=$10,"
            "    custom_mxid=$11, access_token=$12, next_batch=$13, base_url=$14 "
            "WHERE id=$1"
        )
        await self.db.execute(q, *self._values)

    async def insert(self) -> None:
        q = (
            "INSERT INTO puppet ("
            "    id, is_registered, displayname, displayname_source, displayname_contact,"
            "    displayname_quality, disable_updates, username, photo_id, is_bot,"
            "    custom_mxid, access_token, next_batch, base_url"
            ") VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)"
        )
        await self.db.execute(q, *self._values)
