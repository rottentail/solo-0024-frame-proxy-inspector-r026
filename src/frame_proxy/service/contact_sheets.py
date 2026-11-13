from __future__ import annotations

import hashlib

from frame_proxy.config import Settings
from frame_proxy.domain.errors import (
    ApplicationError,
    ErrorCode,
    StateError,
)
from frame_proxy.domain.models import ContactSheetRecord
from frame_proxy.domain.policies import ContactSheetRequest, validate_identifier
from frame_proxy.domain.transitions import transition_contact_sheet
from frame_proxy.media.avi import probe_avi
from frame_proxy.media.contact import compose_grid, plan_grid
from frame_proxy.media.frames import Frame, decode_frame_at
from frame_proxy.media.ppm import validate_ppm
from frame_proxy.storage.repository import FileRepository


class ContactSheetService:
    def __init__(self, repository: FileRepository, settings: Settings) -> None:
        self._repository = repository
        self._settings = settings

    def create(
        self,
        inspection_id: str,
        request: ContactSheetRequest,
    ) -> ContactSheetRecord:
        normalized_id = validate_identifier(inspection_id, "inspection_id")
        inspection = self._repository.get_inspection(normalized_id)
        if inspection.state != "indexed":
            raise StateError(
                "contact sheets require an indexed inspection",
                details={"state": inspection.state},
            )
        clip = self._repository.get_clip(inspection.clip_id)
        plan = plan_grid(
            scene_count=len(inspection.scenes),
            source_width=clip.width,
            source_height=clip.height,
            request=request,
            max_scenes=self._settings.max_sheet_scenes,
            max_pixels=self._settings.max_sheet_pixels,
        )

        record = ContactSheetRecord(
            sheet_id=request.sheet_id,
            inspection_id=inspection.inspection_id,
            state="planned",
            columns=request.columns,
            tile_width=request.tile_width,
            tile_height=plan.tile_height,
            gutter=request.gutter,
            background_rgb=request.background_rgb,
            selection=request.selection,
            rows=plan.rows,
            image_width=plan.image_width,
            image_height=plan.image_height,
            artifact_name="sheet.ppm",
            artifact_digest=None,
            artifact_byte_size=0,
            format_revision=2,
            verification_source="generated",
            tile_map=[],
        )
        self._repository.create_contact_sheet(record)
        transition_contact_sheet(record, "compositing", reason="layout reserved")
        self._repository.update_contact_sheet(record)

        try:
            media = self._repository.get_clip_media(clip.clip_id)
            descriptor = probe_avi(media)
            frames: dict[int, Frame] = {}
            for scene in inspection.scenes:
                if request.selection == "first":
                    frame_index = scene.start_frame
                elif request.selection == "midpoint":
                    frame_index = scene.start_frame + (
                        scene.end_frame - scene.start_frame
                    ) // 2
                else:
                    frame_index = scene.representative_frame
                frames[frame_index] = decode_frame_at(
                    media,
                    descriptor,
                    frame_index,
                )
            result = compose_grid(
                plan=plan,
                scenes=tuple(inspection.scenes),
                frames=frames,
                background_rgb=request.background_rgb,
                selection=request.selection,
            )
            validate_ppm(
                result.payload,
                expected_width=record.image_width,
                expected_height=record.image_height,
            )
            ready_record = ContactSheetRecord.from_dict(record.as_dict())
            ready_record.artifact_digest = result.digest
            ready_record.artifact_byte_size = len(result.payload)
            ready_record.tile_map = list(result.tile_map)
            transition_contact_sheet(
                ready_record,
                "ready",
                reason="artifact verified",
            )
            self._repository.commit_contact_sheet(ready_record, result.payload)
            return ready_record
        except ApplicationError as exc:
            self._mark_failed(record, exc.code.value, exc.message)
            raise
        except Exception as exc:
            self._mark_failed(record, ErrorCode.INTERNAL.value, str(exc))
            raise ApplicationError(
                ErrorCode.INTERNAL,
                "contact sheet composition failed",
                status=500,
            ) from exc

    def _mark_failed(
        self,
        record: ContactSheetRecord,
        code: str,
        message: str,
    ) -> None:
        record.error_code = code
        record.error_message = message
        if record.state in {"planned", "compositing"}:
            transition_contact_sheet(record, "failed", reason=code)
        try:
            self._repository.update_contact_sheet(record)
        except Exception:
            pass

    def get(self, sheet_id: str) -> ContactSheetRecord:
        normalized = validate_identifier(sheet_id, "sheet_id")
        return self._repository.get_contact_sheet(normalized)

    def get_artifact(self, sheet_id: str) -> bytes:
        normalized = validate_identifier(sheet_id, "sheet_id")
        record = self._repository.get_contact_sheet(normalized)
        if record.state != "ready":
            raise StateError(
                "contact sheet is not ready",
                details={"state": record.state},
            )
        payload = self._repository.get_contact_sheet_artifact(
            record.sheet_id,
            record.artifact_name,
        )
        self._verify_published_artifact(record, payload)
        return payload

    def _verify_published_artifact(
        self,
        record: ContactSheetRecord,
        payload: bytes,
    ) -> None:
        if record.artifact_byte_size != len(payload):
            raise ApplicationError(
                ErrorCode.STORAGE,
                "contact sheet artifact byte size is inconsistent",
                status=500,
                details={
                    "sheet_id": record.sheet_id,
                    "expected": record.artifact_byte_size,
                    "actual": len(payload),
                    "format_revision": record.format_revision,
                    "verification_source": record.verification_source,
                },
            )
        if record.artifact_digest is None:
            raise ApplicationError(
                ErrorCode.STORAGE,
                "contact sheet artifact digest is missing",
                status=500,
                details={"sheet_id": record.sheet_id},
            )
        digest = hashlib.sha256(payload).hexdigest()
        if digest != record.artifact_digest:
            raise ApplicationError(
                ErrorCode.STORAGE,
                "contact sheet artifact digest is inconsistent",
                status=500,
                details={"sheet_id": record.sheet_id},
            )
        try:
            validate_ppm(
                payload,
                expected_width=record.image_width,
                expected_height=record.image_height,
            )
        except ValueError as exc:
            raise ApplicationError(
                ErrorCode.STORAGE,
                "contact sheet artifact format is inconsistent",
                status=500,
                details={"sheet_id": record.sheet_id},
            ) from exc
