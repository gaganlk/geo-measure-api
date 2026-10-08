from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Feature, File, FileStatus


class FileRepository:
    """CRUD operations for the File model."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, *, filename: str, file_type: str) -> File:
        file = File(filename=filename, file_type=file_type, status=FileStatus.PROCESSING)
        self._session.add(file)
        await self._session.flush()  # populate id
        return file

    async def get(self, file_id: str) -> File | None:
        return await self._session.get(File, file_id)

    async def get_or_404(self, file_id: str) -> File:
        from app.core.errors import NotFoundError

        file = await self.get(file_id)
        if file is None:
            raise NotFoundError(f"File '{file_id}' not found.")
        return file

    async def list_all(self, *, limit: int = 100, offset: int = 0) -> list[File]:
        result = await self._session.execute(
            select(File).order_by(File.created_at.desc()).limit(limit).offset(offset)
        )
        return list(result.scalars().all())

    async def update_status(
        self,
        file: File,
        *,
        status: FileStatus,
        error: str | None = None,
        feature_count: int | None = None,
        crs: str | None = None,
    ) -> File:
        file.status = status
        if error is not None:
            file.error = error
        if feature_count is not None:
            file.feature_count = feature_count
        if crs is not None:
            file.crs = crs
        self._session.add(file)
        await self._session.flush()
        return file


class FeatureRepository:
    """CRUD operations for the Feature model."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def bulk_create(self, features: list[Feature]) -> None:
        self._session.add_all(features)
        await self._session.flush()

    async def bulk_update(self, features: list[Feature]) -> None:
        """Persist in-place mutations on already-tracked Feature objects."""
        for feat in features:
            self._session.add(feat)
        await self._session.flush()

    async def list_for_file(self, file_id: str) -> list[Feature]:
        result = await self._session.execute(
            select(Feature).where(Feature.file_id == file_id).order_by(Feature.feature_index)
        )
        return list(result.scalars().all())

    async def paginate_for_file(
        self,
        file_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[int, list[Feature]]:
        """Return (total_count, page) for *file_id*."""
        count_result = await self._session.execute(
            select(func.count()).select_from(Feature).where(Feature.file_id == file_id)
        )
        total = count_result.scalar_one()

        rows_result = await self._session.execute(
            select(Feature)
            .where(Feature.file_id == file_id)
            .order_by(Feature.feature_index)
            .limit(limit)
            .offset(offset)
        )
        return total, list(rows_result.scalars().all())

    async def get(self, feature_id: int) -> Feature | None:
        return await self._session.get(Feature, feature_id)
