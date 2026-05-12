"""Data models for Video Studio pipeline."""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional


class JobPhase(str, Enum):
    QUEUED = "queued"
    SCENARIO = "scenario"
    ASSETS = "assets"
    VERIFICATION = "verification"
    MUSIC = "music"
    ANIMATION = "animation"
    DIRECTOR_QC = "director_qc"
    ASSEMBLY = "assembly"
    DONE = "done"
    ERROR = "error"


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"


class Effort(str, Enum):
    LOW = "low"         # 1 candidate, 0 retries, no Gemini QC
    REGULAR = "regular" # 2 candidates parallel, 1 retry, no Gemini QC
    MAX = "max"         # 3 candidates parallel, 2+ retries, full Gemini AV QC + Director pass


@dataclass
class VerificationResult:
    """Result of VLM verification of a generated asset."""
    passed: bool = False
    issues: list[str] = field(default_factory=list)
    suggestion: str = ""
    retry_count: int = 0


@dataclass
class SceneQualityReport:
    """Multi-dimensional quality report for a generated video scene (Gemini AV QC)."""
    scene_index: int = 0
    visual_score: float = 0.0
    audio_score: float = 5.0   # default 5 = not evaluated
    av_sync_score: float = 5.0
    identity_score: float = 0.0
    motion_score: float = 0.0
    artifacts_score: float = 0.0
    issues: list[str] = field(default_factory=list)
    passed: bool = False
    retry_count: int = 0
    suggestion: str = ""


@dataclass
class Character:
    name: str
    description: str
    visual_traits: str  # detailed visual description for image gen
    sheet_url: Optional[str] = None  # generated character reference image URL


@dataclass
class Location:
    name: str
    description: str
    visual_traits: str
    art_url: Optional[str] = None  # generated location concept art URL


@dataclass
class Scene:
    index: int
    description: str
    duration_sec: float  # 4-15 seconds
    characters: list[str]  # character names involved
    location: str  # location name
    camera_direction: str  # e.g. "slow dolly forward, 85mm lens", "close-up pan"
    dialogue: Optional[str] = None  # spoken dialogue text
    mood: str = "neutral"
    transition_from: Optional[str] = None  # how this scene connects from previous
    keyframe_url: Optional[str] = None  # generated keyframe image
    video_url: Optional[str] = None  # generated video clip URL
    audio_url: Optional[str] = None  # scene audio (TTS dialogue)
    prev_frame_url: Optional[str] = None  # last frame of previous scene for continuity
    lens_type: Optional[str] = None  # e.g. "85mm anamorphic"
    color_temperature: Optional[str] = None  # e.g. "warm golden hour"
    lighting_setup: Optional[str] = None  # e.g. "natural window light, soft shadows"
    causal_link: Optional[str] = None  # why this scene follows causally from the previous one


@dataclass
class MusicCue:
    segment_index: int
    mood: str
    tempo: str  # "slow", "medium", "fast"
    style: str  # "orchestral", "electronic", "acoustic", etc.
    duration_sec: float
    description: str  # text description for music gen
    audio_url: Optional[str] = None  # generated music clip URL


@dataclass
class Storyboard:
    title: str
    synopsis: str
    style: str  # cinematic sub-style
    total_duration_sec: float
    characters: list[Character] = field(default_factory=list)
    locations: list[Location] = field(default_factory=list)
    scenes: list[Scene] = field(default_factory=list)
    music_cues: list[MusicCue] = field(default_factory=list)


@dataclass
class GenerationSettings:
    theme: str = ""
    style: str = "photorealistic cinematic"
    duration_sec: float = 30.0
    num_scenes: int = 4
    mood: str = "dramatic"
    resolution: str = "720p"
    aspect_ratio: str = "16:9"
    video_model: str = "bytedance/seedance-2.0"
    image_model: str = "nanobanana"  # nanobanana (gemini flash) is better for photorealism
    include_dialogue: bool = True
    include_music: bool = True
    music_style: str = "orchestral cinematic"
    effort: str = "regular"   # "low" | "regular" | "max"
    generate_audio: bool = True  # pass generate_audio=true to Seedance for voice synthesis


@dataclass
class JobProgress:
    phase: JobPhase = JobPhase.QUEUED
    status: JobStatus = JobStatus.QUEUED
    progress_pct: float = 0.0
    message: str = ""
    # Intermediate results
    storyboard: Optional[Storyboard] = None
    character_sheets: list[str] = field(default_factory=list)
    location_arts: list[str] = field(default_factory=list)
    keyframes: list[str] = field(default_factory=list)
    music_clips: list[str] = field(default_factory=list)
    video_clips: list[str] = field(default_factory=list)
    final_video_url: Optional[str] = None
    error: Optional[str] = None
    warnings: list[str] = field(default_factory=list)
    verification_stats: dict = field(default_factory=dict)
    quality_reports: list = field(default_factory=list)  # list of SceneQualityReport dicts


@dataclass
class Job:
    job_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    settings: GenerationSettings = field(default_factory=GenerationSettings)
    progress: JobProgress = field(default_factory=JobProgress)
    created_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, data: dict) -> "Job":
        settings_data = data.get("settings", {})
        known_fields = {f for f in GenerationSettings.__dataclass_fields__}
        filtered_settings = {k: v for k, v in settings_data.items() if k in known_fields}
        settings = GenerationSettings(**filtered_settings)

        progress_data = data.get("progress", {})
        storyboard = None
        if progress_data.get("storyboard"):
            sb = progress_data["storyboard"]
            storyboard = Storyboard(
                title=sb.get("title", ""),
                synopsis=sb.get("synopsis", ""),
                style=sb.get("style", ""),
                total_duration_sec=sb.get("total_duration_sec", 0),
                characters=[Character(**c) for c in sb.get("characters", [])],
                locations=[Location(**loc) for loc in sb.get("locations", [])],
                scenes=[Scene(**{k: v for k, v in s.items() if k in Scene.__dataclass_fields__}) for s in sb.get("scenes", [])],
                music_cues=[MusicCue(**m) for m in sb.get("music_cues", [])],
            )
        progress = JobProgress(
            phase=JobPhase(progress_data.get("phase", "queued")),
            status=JobStatus(progress_data.get("status", "queued")),
            progress_pct=progress_data.get("progress_pct", 0.0),
            message=progress_data.get("message", ""),
            storyboard=storyboard,
            character_sheets=progress_data.get("character_sheets", []),
            location_arts=progress_data.get("location_arts", []),
            keyframes=progress_data.get("keyframes", []),
            music_clips=progress_data.get("music_clips", []),
            video_clips=progress_data.get("video_clips", []),
            final_video_url=progress_data.get("final_video_url"),
            error=progress_data.get("error"),
            warnings=progress_data.get("warnings", []),
            verification_stats=progress_data.get("verification_stats", {}),
            quality_reports=progress_data.get("quality_reports", []),
        )
        return cls(
            job_id=data.get("job_id", str(uuid.uuid4())[:8]),
            settings=settings,
            progress=progress,
            created_at=data.get("created_at", ""),
        )
