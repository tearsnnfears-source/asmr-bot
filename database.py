from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import BigInteger, Integer, String, DateTime, Boolean, Text, text, func
from datetime import datetime
import os
import re
import logging

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./asmr.db")

if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://")

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id:                  Mapped[int]      = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id:         Mapped[int]      = mapped_column(BigInteger, unique=True, index=True)
    username:            Mapped[str|None] = mapped_column(String(64), nullable=True)
    full_name:           Mapped[str|None] = mapped_column(String(256), nullable=True)
    lang:                Mapped[str]      = mapped_column(String(4), default="")
    units:               Mapped[int]      = mapped_column(Integer, default=0)
    is_active:           Mapped[bool]     = mapped_column(Boolean, default=False)
    trial_used:          Mapped[bool]     = mapped_column(Boolean, default=False)
    notify_expiry:       Mapped[bool]     = mapped_column(Boolean, default=True)
    last_payment_method: Mapped[str|None] = mapped_column(String(32), nullable=True)
    badge:               Mapped[str|None] = mapped_column(String(256), nullable=True)
    tier:                Mapped[str]      = mapped_column(String(16), default='plus')
    created_at:          Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PendingPayment(Base):
    __tablename__ = "pending_payments"

    id:          Mapped[int]      = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int]      = mapped_column(BigInteger, index=True)
    units:       Mapped[int]      = mapped_column(Integer)
    amount:      Mapped[int]      = mapped_column(Integer)
    label:       Mapped[str]      = mapped_column(String(64))
    method:      Mapped[str]      = mapped_column(String(32))
    created_at:  Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PendingInvite(Base):
    __tablename__ = "pending_invites"

    id:          Mapped[int]      = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int]      = mapped_column(BigInteger, index=True)
    invite_link: Mapped[str]      = mapped_column(Text)
    used:        Mapped[bool]     = mapped_column(Boolean, default=False)
    created_at:  Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Favorite(Base):
    __tablename__ = "favorites"

    id:          Mapped[int]      = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int]      = mapped_column(BigInteger, index=True)
    content_id:  Mapped[int|None] = mapped_column(Integer, nullable=True, index=True)  # ArtistContent.id
    title:       Mapped[str]      = mapped_column(String(128))
    url:         Mapped[str]      = mapped_column(Text)
    created_at:  Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Playlist(Base):
    __tablename__ = "playlists"

    id:          Mapped[int]      = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int]      = mapped_column(BigInteger, index=True)
    name:        Mapped[str]      = mapped_column(String(128))
    created_at:  Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PlaylistItem(Base):
    __tablename__ = "playlist_items"

    id:          Mapped[int]      = mapped_column(Integer, primary_key=True, autoincrement=True)
    playlist_id: Mapped[int]      = mapped_column(Integer, index=True)
    content_id:  Mapped[int]      = mapped_column(Integer, index=True)
    created_at:  Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ArtistSuggestion(Base):
    __tablename__ = "artist_suggestions"

    id:          Mapped[int]      = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int]      = mapped_column(BigInteger)
    username:    Mapped[str|None] = mapped_column(String(64), nullable=True)
    artist_name: Mapped[str]      = mapped_column(String(256))
    created_at:  Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ArtistContent(Base):
    __tablename__ = "artist_content"

    id:            Mapped[int]      = mapped_column(Integer, primary_key=True, autoincrement=True)
    artist_name:   Mapped[str]      = mapped_column(String(128), index=True)
    content_type:  Mapped[str]      = mapped_column(String(8))   # "video" | "photo"
    title:         Mapped[str|None] = mapped_column(String(256), nullable=True)
    url:           Mapped[str]      = mapped_column(Text)
    thumbnail_url: Mapped[str|None] = mapped_column(Text, nullable=True)
    tags:          Mapped[str|None] = mapped_column(String(256), nullable=True)
    sort_order:    Mapped[int]      = mapped_column(Integer, default=0)
    created_at:    Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Tag(Base):
    __tablename__ = "tags"

    id:         Mapped[int]      = mapped_column(Integer, primary_key=True, autoincrement=True)
    name:       Mapped[str]      = mapped_column(String(64), unique=True, index=True)
    color:      Mapped[str]      = mapped_column(String(16), default="#FFFFFF")  # hex
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class VideoReaction(Base):
    __tablename__ = "video_reactions"

    id:          Mapped[int]      = mapped_column(Integer, primary_key=True, autoincrement=True)
    content_id:  Mapped[int]      = mapped_column(Integer, index=True)
    telegram_id: Mapped[int]      = mapped_column(BigInteger, index=True)
    emoji:       Mapped[str]      = mapped_column(String(8))
    created_at:  Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class VideoComment(Base):
    __tablename__ = "video_comments"

    id:          Mapped[int]      = mapped_column(Integer, primary_key=True, autoincrement=True)
    content_id:  Mapped[int]      = mapped_column(Integer, index=True)
    telegram_id: Mapped[int]      = mapped_column(BigInteger, index=True)
    username:    Mapped[str|None] = mapped_column(String(64), nullable=True)
    photo_url:   Mapped[str|None] = mapped_column(Text, nullable=True)
    text:        Mapped[str]      = mapped_column(Text)
    created_at:  Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Artist(Base):
    __tablename__ = "artists"

    id:                Mapped[int]      = mapped_column(Integer, primary_key=True, autoincrement=True)
    name:              Mapped[str]      = mapped_column(String(128), unique=True, index=True)
    photo_url:         Mapped[str|None] = mapped_column(String(512), nullable=True)
    profile_photo_url: Mapped[str|None] = mapped_column(String(512), nullable=True)
    topic_url:         Mapped[str|None] = mapped_column(String(512), nullable=True)
    photos:            Mapped[int]      = mapped_column(Integer, default=0)
    videos:            Mapped[int]      = mapped_column(Integer, default=0)
    tag_hot:           Mapped[bool]     = mapped_column(Boolean, default=False)
    tag_new:           Mapped[bool]     = mapped_column(Boolean, default=False)
    tag_prom:          Mapped[bool]     = mapped_column(Boolean, default=False)
    tag_ready:         Mapped[bool]     = mapped_column(Boolean, default=False)
    created_at:        Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


def _bunny_thumbnail(embed_url: str | None) -> str | None:
    """Генерирует URL превью из Bunny Stream embed_url."""
    if not embed_url:
        return None
    m = re.search(r'embed/(\d+)/([a-f0-9-]+)', embed_url, re.I)
    if not m:
        return None
    library_id, video_id = m.group(1), m.group(2)
    return f"https://iframe.mediadelivery.net/thumbnail/{library_id}/{video_id}"


def _auto_thumbnail(url: str | None) -> str | None:
    """Пытается сгенерировать URL превью из любого Bunny CDN URL."""
    if not url:
        return None
    # Bunny embed: iframe.mediadelivery.net/embed/{lib}/{id}
    m = re.search(r'embed/(\d+)/([a-f0-9-]+)', url, re.I)
    if m:
        return f"https://iframe.mediadelivery.net/thumbnail/{m.group(1)}/{m.group(2)}"
    # Bunny CDN stream: vz-xxx.b-cdn.net/{video_id}/playlist.m3u8
    m = re.search(r'(https://[^/]+\.b-cdn\.net)/([a-f0-9-]+)/playlist\.m3u8', url, re.I)
    if m:
        return f"{m.group(1)}/{m.group(2)}/thumbnail.jpg"
    # Bunny CDN generic: vz-xxx.b-cdn.net/{video_id}/...
    m = re.search(r'(https://[^/]+\.b-cdn\.net)/([a-f0-9-]{36})/', url, re.I)
    if m:
        return f"{m.group(1)}/{m.group(2)}/thumbnail.jpg"
    return None


class Video(Base):
    __tablename__ = "videos"

    id:            Mapped[int]      = mapped_column(Integer, primary_key=True, autoincrement=True)
    title:         Mapped[str]      = mapped_column(String(128))
    url:           Mapped[str]      = mapped_column(String(512))
    embed_url:     Mapped[str]      = mapped_column(String(512))
    thumbnail_url: Mapped[str|None] = mapped_column(String(512), nullable=True)
    artist_name:   Mapped[str]      = mapped_column(String(128), index=True)
    duration:      Mapped[str|None] = mapped_column(String(16), nullable=True)
    is_active:     Mapped[bool]     = mapped_column(Boolean, default=True)
    created_at:    Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    if "postgresql" in DATABASE_URL:
        migrations = [
            "ALTER TABLE artists ADD COLUMN IF NOT EXISTS photo_url VARCHAR(512)",
            "ALTER TABLE artists ADD COLUMN IF NOT EXISTS profile_photo_url VARCHAR(512)",
            "ALTER TABLE artists ADD COLUMN IF NOT EXISTS photos INTEGER DEFAULT 0",
            "ALTER TABLE artists ADD COLUMN IF NOT EXISTS videos INTEGER DEFAULT 0",
            "ALTER TABLE artists ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW()",
            "ALTER TABLE artists ADD COLUMN IF NOT EXISTS tag_hot BOOLEAN DEFAULT FALSE",
            "ALTER TABLE artists ADD COLUMN IF NOT EXISTS tag_new BOOLEAN DEFAULT FALSE",
            "ALTER TABLE artists ADD COLUMN IF NOT EXISTS tag_prom BOOLEAN DEFAULT FALSE",
            "ALTER TABLE artists ADD COLUMN IF NOT EXISTS tag_ready BOOLEAN DEFAULT FALSE",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS notify_expiry BOOLEAN DEFAULT TRUE",
            "ALTER TABLE videos ADD COLUMN IF NOT EXISTS thumbnail_url VARCHAR(512)",
            "ALTER TABLE artists ADD COLUMN IF NOT EXISTS topic_url VARCHAR(512)",
            "CREATE TABLE IF NOT EXISTS artist_content (id SERIAL PRIMARY KEY, artist_name VARCHAR(128), content_type VARCHAR(8), title VARCHAR(256), url TEXT, thumbnail_url TEXT, tags VARCHAR(256), sort_order INTEGER DEFAULT 0, created_at TIMESTAMP DEFAULT NOW())",
            "ALTER TABLE artist_content ADD COLUMN IF NOT EXISTS artist_name VARCHAR(128)",
            "ALTER TABLE artist_content ADD COLUMN IF NOT EXISTS content_type VARCHAR(8)",
            "ALTER TABLE artist_content ADD COLUMN IF NOT EXISTS title VARCHAR(256)",
            "ALTER TABLE artist_content ADD COLUMN IF NOT EXISTS url TEXT",
            "ALTER TABLE artist_content ADD COLUMN IF NOT EXISTS thumbnail_url TEXT",
            "ALTER TABLE artist_content ADD COLUMN IF NOT EXISTS tags VARCHAR(256)",
            "ALTER TABLE artist_content ADD COLUMN IF NOT EXISTS sort_order INTEGER DEFAULT 0",
            "ALTER TABLE artist_content ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW()",
            "CREATE INDEX IF NOT EXISTS idx_artist_content_name ON artist_content (artist_name)",
            "CREATE TABLE IF NOT EXISTS tags (id SERIAL PRIMARY KEY, name VARCHAR(64) UNIQUE, color VARCHAR(16) DEFAULT '#FFFFFF', created_at TIMESTAMP DEFAULT NOW())",
            "CREATE TABLE IF NOT EXISTS video_reactions (id SERIAL PRIMARY KEY, content_id INTEGER, telegram_id BIGINT, emoji VARCHAR(8), created_at TIMESTAMP DEFAULT NOW())",
            "CREATE INDEX IF NOT EXISTS idx_reactions_content ON video_reactions (content_id)",
            "CREATE TABLE IF NOT EXISTS video_comments (id SERIAL PRIMARY KEY, content_id INTEGER, telegram_id BIGINT, username VARCHAR(64), photo_url TEXT, text TEXT, created_at TIMESTAMP DEFAULT NOW())",
            "ALTER TABLE video_comments ADD COLUMN IF NOT EXISTS photo_url TEXT",
            "CREATE INDEX IF NOT EXISTS idx_comments_content ON video_comments (content_id)",
            "ALTER TABLE favorites ADD COLUMN IF NOT EXISTS content_id INTEGER",
            "CREATE TABLE IF NOT EXISTS playlists (id SERIAL PRIMARY KEY, telegram_id BIGINT, name VARCHAR(128), created_at TIMESTAMP DEFAULT NOW())",
            "CREATE INDEX IF NOT EXISTS idx_playlists_user ON playlists (telegram_id)",
            "CREATE TABLE IF NOT EXISTS playlist_items (id SERIAL PRIMARY KEY, playlist_id INTEGER, content_id INTEGER, created_at TIMESTAMP DEFAULT NOW())",
            "CREATE INDEX IF NOT EXISTS idx_playlist_items_pl ON playlist_items (playlist_id)",
            "CREATE TABLE IF NOT EXISTS artist_suggestions (id SERIAL PRIMARY KEY, telegram_id BIGINT, username VARCHAR(64), artist_name VARCHAR(256), created_at TIMESTAMP DEFAULT NOW())",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS badge VARCHAR(256)",
            "DO $$ BEGIN IF EXISTS(SELECT 1 FROM information_schema.columns WHERE table_name='users' AND column_name='badge' AND character_maximum_length=32) THEN ALTER TABLE users ALTER COLUMN badge TYPE VARCHAR(256); END IF; END $$",
            "CREATE TABLE IF NOT EXISTS custom_badges (id SERIAL PRIMARY KEY, name VARCHAR(32) UNIQUE, color VARCHAR(16), created_at TIMESTAMP DEFAULT NOW())",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS tier VARCHAR(16) DEFAULT 'plus'",
            "CREATE TABLE IF NOT EXISTS crypto_checkouts (id SERIAL PRIMARY KEY, order_uuid VARCHAR(64) UNIQUE, telegram_id BIGINT, tier VARCHAR(16) DEFAULT 'plus', crypto_amount NUMERIC(12,4), crypto_currency VARCHAR(16), network VARCHAR(32), wallet_address TEXT, tx_hash TEXT, status VARCHAR(16) DEFAULT 'pending', expires_at TIMESTAMP, created_at TIMESTAMP DEFAULT NOW())",
            "CREATE INDEX IF NOT EXISTS idx_crypto_checkouts_user ON crypto_checkouts (telegram_id)",
            "CREATE TABLE IF NOT EXISTS watch_history (id SERIAL PRIMARY KEY, telegram_id BIGINT, content_id INTEGER, progress_seconds INTEGER DEFAULT 0, duration_seconds INTEGER DEFAULT 0, updated_at TIMESTAMP DEFAULT NOW(), UNIQUE(telegram_id, content_id))",
            "CREATE INDEX IF NOT EXISTS idx_watch_history_user ON watch_history (telegram_id, updated_at DESC)",
            "INSERT INTO custom_badges (name, color) VALUES ('PLUS', '#FF7EC8') ON CONFLICT (name) DO NOTHING",
            "INSERT INTO custom_badges (name, color) VALUES ('PRO', '#00E5FF') ON CONFLICT (name) DO NOTHING",
            "INSERT INTO custom_badges (name, color) VALUES ('ELITE', '#FFD700') ON CONFLICT (name) DO NOTHING",
            "CREATE TABLE IF NOT EXISTS pending_invites (id SERIAL PRIMARY KEY, telegram_id BIGINT, invite_link TEXT, used BOOLEAN DEFAULT FALSE, created_at TIMESTAMP DEFAULT NOW())",
            "CREATE INDEX IF NOT EXISTS idx_pending_invites_user ON pending_invites (telegram_id)",
        ]
        for sql in migrations:
            try:
                async with engine.begin() as conn:
                    await conn.execute(text(sql))
            except Exception as e:
                logger.warning(f"Migration warning for {sql!r}: {e}")

    # Заполняем thumbnail_url для старых видео без превью
    await _backfill_thumbnails()


async def _backfill_thumbnails():
    """Проставляет thumbnail_url для видео и контента где его нет."""
    from sqlalchemy import select
    try:
        async with async_session() as session:
            # Old Video table
            result = await session.execute(
                select(Video).where(Video.thumbnail_url == None, Video.is_active == True)
            )
            videos = result.scalars().all()
            updated = 0
            for video in videos:
                thumb = _bunny_thumbnail(video.embed_url)
                if thumb:
                    video.thumbnail_url = thumb
                    updated += 1

            # ArtistContent (shorts, videos, photos)
            result2 = await session.execute(
                select(ArtistContent).where(ArtistContent.thumbnail_url == None)
            )
            contents = result2.scalars().all()
            for item in contents:
                thumb = _auto_thumbnail(item.url)
                if thumb:
                    item.thumbnail_url = thumb
                    updated += 1

            if updated:
                await session.commit()
                logger.info(f"Backfilled thumbnails for {updated} items")
    except Exception as e:
        logger.warning(f"Thumbnail backfill error: {e}")


async def get_user(session: AsyncSession, telegram_id: int) -> User | None:
    from sqlalchemy import select
    result = await session.execute(select(User).where(User.telegram_id == telegram_id))
    return result.scalar_one_or_none()


async def get_or_create_user(session: AsyncSession, telegram_id: int, username: str = None, full_name: str = None) -> User:
    user = await get_user(session, telegram_id)
    if not user:
        user = User(
            telegram_id=telegram_id,
            username=username,
            full_name=full_name,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
    return user


# ─── Artist functions ─────────────────────────────────────────────────────────

async def get_artist(session: AsyncSession, name: str) -> Artist | None:
    from sqlalchemy import select
    result = await session.execute(select(Artist).where(Artist.name == name))
    return result.scalar_one_or_none()


async def get_all_artists(session: AsyncSession) -> list[Artist]:
    from sqlalchemy import select
    result = await session.execute(select(Artist).order_by(Artist.name))
    return list(result.scalars().all())


async def create_artist(session: AsyncSession, name: str, photo_url: str = None,
                        profile_photo_url: str = None, photos: int = 0, videos: int = 0) -> Artist:
    artist = Artist(
        name=name,
        photo_url=photo_url,
        profile_photo_url=profile_photo_url,
        photos=photos,
        videos=videos,
    )
    session.add(artist)
    await session.commit()
    await session.refresh(artist)
    return artist


async def delete_artist(session: AsyncSession, name: str) -> bool:
    artist = await get_artist(session, name)
    if artist:
        await session.delete(artist)
        await session.commit()
        return True
    return False


async def update_artist_stats(session: AsyncSession, name: str,
                               photos: int = None, videos: int = None) -> Artist | None:
    artist = await get_artist(session, name)
    if not artist:
        return None
    if photos is not None:
        artist.photos = photos
    if videos is not None:
        artist.videos = videos
    await session.commit()
    await session.refresh(artist)
    return artist


async def set_artist_topic_url(session: AsyncSession, name: str, url: str) -> Artist | None:
    """Set group topic URL for an artist."""
    artist = await get_artist(session, name)
    if not artist:
        return None
    artist.topic_url = url
    await session.commit()
    await session.refresh(artist)
    return artist


async def set_artist_tag(session: AsyncSession, name: str, tag: str, value: bool) -> Artist | None:
    artist = await get_artist(session, name)
    if not artist:
        return None
    if tag == 'hot':
        artist.tag_hot = value
    elif tag == 'new':
        artist.tag_new = value
    elif tag == 'prom':
        artist.tag_prom = value
    elif tag == 'ready':
        artist.tag_ready = value
    await session.commit()
    await session.refresh(artist)
    return artist


# ─── Video functions ──────────────────────────────────────────────────────────

async def get_all_videos(session: AsyncSession, limit: int = 20) -> list[Video]:
    from sqlalchemy import select
    result = await session.execute(
        select(Video).where(Video.is_active == True)
        .order_by(Video.created_at.desc()).limit(limit)
    )
    return list(result.scalars().all())


async def create_video(session: AsyncSession, title: str, url: str, embed_url: str,
                       artist_name: str, duration: str = None,
                       thumbnail_url: str = None) -> Video:
    # Автогенерация thumbnail если не передан явно
    if not thumbnail_url:
        thumbnail_url = _bunny_thumbnail(embed_url)

    video = Video(
        title=title,
        url=url,
        embed_url=embed_url,
        thumbnail_url=thumbnail_url,
        artist_name=artist_name,
        duration=duration,
    )
    session.add(video)
    await session.commit()
    await session.refresh(video)
    return video


async def delete_video(session: AsyncSession, video_id: int) -> bool:
    from sqlalchemy import select
    result = await session.execute(select(Video).where(Video.id == video_id))
    video = result.scalar_one_or_none()
    if video:
        await session.delete(video)
        await session.commit()
        return True
    return False


# ─── ArtistContent CRUD ───────────────────────────────────────────────────────

async def get_artist_content(session: AsyncSession, artist_name: str,
                              content_type: str | None = None) -> list[ArtistContent]:
    from sqlalchemy import select
    q = select(ArtistContent).where(ArtistContent.artist_name == artist_name)
    if content_type:
        q = q.where(ArtistContent.content_type == content_type)
    q = q.order_by(ArtistContent.sort_order, ArtistContent.created_at.desc())
    result = await session.execute(q)
    return list(result.scalars().all())


async def add_artist_content(session: AsyncSession, artist_name: str, content_type: str,
                              url: str, title: str | None = None, tags: str | None = None,
                              sort_order: int = 0, thumbnail_url: str | None = None) -> ArtistContent:
    if not thumbnail_url:
        thumbnail_url = _auto_thumbnail(url)
    item = ArtistContent(
        artist_name=artist_name, content_type=content_type,
        url=url, title=title, tags=tags, sort_order=sort_order,
        thumbnail_url=thumbnail_url,
    )
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item


async def clear_artist_content(session: AsyncSession, artist_name: str,
                                content_type: str | None = None) -> int:
    from sqlalchemy import delete
    q = delete(ArtistContent).where(ArtistContent.artist_name == artist_name)
    if content_type:
        q = q.where(ArtistContent.content_type == content_type)
    result = await session.execute(q)
    await session.commit()
    return result.rowcount


async def get_artist_content_counts(session: AsyncSession, artist_name: str) -> dict:
    from sqlalchemy import select, func
    result = await session.execute(
        select(ArtistContent.content_type, func.count(ArtistContent.id))
        .where(ArtistContent.artist_name == artist_name)
        .group_by(ArtistContent.content_type)
    )
    counts = {"video": 0, "photo": 0}
    for ct, cnt in result.all():
        counts[ct] = cnt
    return counts


# ─── Tag CRUD ─────────────────────────────────────────────────────────────────

async def get_all_tags(session: AsyncSession) -> list[Tag]:
    from sqlalchemy import select
    result = await session.execute(select(Tag).order_by(Tag.name))
    return list(result.scalars().all())


async def get_tag(session: AsyncSession, name: str) -> Tag | None:
    from sqlalchemy import select
    result = await session.execute(select(Tag).where(Tag.name == name))
    return result.scalar_one_or_none()


async def create_tag(session: AsyncSession, name: str, color: str) -> Tag:
    tag = Tag(name=name, color=color)
    session.add(tag)
    await session.commit()
    await session.refresh(tag)
    return tag


async def delete_tag(session: AsyncSession, name: str) -> bool:
    tag = await get_tag(session, name)
    if tag:
        await session.delete(tag)
        await session.commit()
        return True
    return False


# ─── CustomBadge CRUD ────────────────────────────────────────────────────────

class CustomBadge(Base):
    __tablename__ = "custom_badges"

    id:         Mapped[int]      = mapped_column(Integer, primary_key=True, autoincrement=True)
    name:       Mapped[str]      = mapped_column(String(32), unique=True, index=True)
    color:      Mapped[str]      = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


async def create_custom_badge(session: AsyncSession, name: str, color: str) -> CustomBadge:
    badge = CustomBadge(name=name, color=color)
    session.add(badge)
    await session.commit()
    await session.refresh(badge)
    return badge


async def get_custom_badges(session: AsyncSession) -> list[CustomBadge]:
    from sqlalchemy import select
    result = await session.execute(select(CustomBadge).order_by(CustomBadge.name))
    return list(result.scalars().all())


async def delete_custom_badge(session: AsyncSession, name: str) -> bool:
    from sqlalchemy import select
    result = await session.execute(select(CustomBadge).where(CustomBadge.name == name))
    badge = result.scalar_one_or_none()
    if badge:
        await session.delete(badge)
        await session.commit()
        return True
    return False


# ─── Tier badge auto-assign ───────────────────────────────────────────────────

TIER_BADGE_NAMES = {'PLUS', 'PRO', 'ELITE'}

def assign_tier_badge(user: "User") -> None:
    """Remove old tier badges and set the correct one for user.tier. Mutates user.badge."""
    tier_to_badge = {'plus': 'PLUS', 'pro': 'PRO', 'elite': 'ELITE'}
    new_badge = tier_to_badge.get((user.tier or 'plus').lower())
    current = [b.strip() for b in (user.badge or '').split(',') if b.strip()]
    current = [b for b in current if b not in TIER_BADGE_NAMES]
    if new_badge:
        current.append(new_badge)
    user.badge = ','.join(current) if current else None


# ─── PendingInvite CRUD ───────────────────────────────────────────────────────

async def create_pending_invite(session: AsyncSession, telegram_id: int, invite_link: str) -> PendingInvite:
    invite = PendingInvite(telegram_id=telegram_id, invite_link=invite_link)
    session.add(invite)
    await session.commit()
    await session.refresh(invite)
    return invite


async def consume_pending_invite(session: AsyncSession, telegram_id: int) -> str | None:
    """Return and mark-as-used the newest unused invite link for a user."""
    from sqlalchemy import select
    result = await session.execute(
        select(PendingInvite)
        .where(PendingInvite.telegram_id == telegram_id, PendingInvite.used == False)
        .order_by(PendingInvite.created_at.desc())
        .limit(1)
    )
    invite = result.scalar_one_or_none()
    if not invite:
        return None
    invite.used = True
    await session.commit()
    return invite.invite_link


# ─── Reaction CRUD ────────────────────────────────────────────────────────────

ALLOWED_REACTIONS = ["🔥","❤️","👅","💦","😍","🥵","🫦","😮","🤤","💤","😴","✨","🎧","💋","🤫"]
MAX_REACTIONS_PER_USER = 3


async def get_reactions(session: AsyncSession, content_id: int) -> dict:
    """Returns {emoji: count, ...}."""
    from sqlalchemy import select, func
    result = await session.execute(
        select(VideoReaction.emoji, func.count(VideoReaction.id))
        .where(VideoReaction.content_id == content_id)
        .group_by(VideoReaction.emoji)
    )
    counts = {r[0]: r[1] for r in result.all()}
    return counts


async def get_user_reactions(session: AsyncSession, content_id: int, telegram_id: int) -> list[str]:
    """Returns list of emojis user has reacted with."""
    from sqlalchemy import select
    result = await session.execute(
        select(VideoReaction.emoji).where(
            VideoReaction.content_id == content_id,
            VideoReaction.telegram_id == telegram_id,
        )
    )
    return [r[0] for r in result.all()]


async def get_user_reaction(session: AsyncSession, content_id: int, telegram_id: int) -> str | None:
    """Legacy single-reaction getter (returns first or None)."""
    reactions = await get_user_reactions(session, content_id, telegram_id)
    return reactions[0] if reactions else None


async def set_reaction(session: AsyncSession, content_id: int, telegram_id: int, emoji: str) -> dict:
    """Toggle reaction — same emoji removes it; adding new one allowed up to MAX_REACTIONS_PER_USER."""
    from sqlalchemy import select
    result = await session.execute(
        select(VideoReaction).where(
            VideoReaction.content_id == content_id,
            VideoReaction.telegram_id == telegram_id,
            VideoReaction.emoji == emoji,
        )
    )
    existing = result.scalar_one_or_none()
    if existing:
        await session.delete(existing)   # toggle off
    else:
        # Check limit
        count_result = await session.execute(
            select(func.count(VideoReaction.id)).where(
                VideoReaction.content_id == content_id,
                VideoReaction.telegram_id == telegram_id,
            )
        )
        current_count = count_result.scalar() or 0
        if current_count < MAX_REACTIONS_PER_USER:
            session.add(VideoReaction(content_id=content_id, telegram_id=telegram_id, emoji=emoji))
    await session.commit()
    return await get_reactions(session, content_id)


# ─── Comment CRUD ─────────────────────────────────────────────────────────────

async def get_comments(session: AsyncSession, content_id: int, limit: int = 50) -> list[VideoComment]:
    from sqlalchemy import select
    result = await session.execute(
        select(VideoComment)
        .where(VideoComment.content_id == content_id)
        .order_by(VideoComment.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def add_comment(session: AsyncSession, content_id: int, telegram_id: int,
                      username: str | None, text: str,
                      photo_url: str | None = None) -> VideoComment:
    comment = VideoComment(content_id=content_id, telegram_id=telegram_id,
                           username=username, text=text[:500], photo_url=photo_url)
    session.add(comment)
    await session.commit()
    await session.refresh(comment)
    return comment
