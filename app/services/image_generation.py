"""Image generation service using Google Gemini API.

Handles player portrait generation with S3 storage and database auditing.
Designed for reuse from both CLI scripts and future admin UI.
"""

import logging
import time
from datetime import datetime
from typing import Optional

from google import genai
from google.genai import types
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.schemas.image_snapshots import PlayerImageAsset, PlayerImageSnapshot
from app.schemas.players_master import PlayerMaster
from app.services.s3_client import s3_client

logger = logging.getLogger(__name__)

# Default system prompt for DraftGuru portrait generation
DEFAULT_SYSTEM_PROMPT = """You are a "DraftGuru Portrait Illustrator".

Goal:
Generate a HERO portrait image for a basketball player that visually matches DraftGuru's UI theme:
clean, bright, modern-retro, with slate neutrals, a primary blue, and a subtle cyan accent.

Output:
- 1 PNG image
- Size: 800x1000 (4:5)
- Composition: chest-up portrait, centered; shoulders visible; head not cropped
- Must read clearly at thumbnail size

Style:
- Flat vector poster / illustrated look (NOT photorealistic)
- Clean, simple shapes; 2–5 main fill layers for the subject
- Optional subtle outline using slate tones (prefer #334155 or #1e293b)
- Very light grain allowed (paper texture feel), but keep it minimal and NEVER on the face

DraftGuru palette (use ONLY these colors and their light/dark tints):
Primary UI colors:
- #4A7FB8 (primary blue)
- #E8B4A8 (secondary peach)
Accent:
- #06b6d4 (cyan) — use subtly (<5% of pixels), mostly in background motif and small trim
Neutrals:
- #ffffff, #f8fafc, #f1f5f9, #e2e8f0, #cbd5e1
- #64748b, #475569, #334155, #1e293b, #0f172a

Color roles (must follow):
Skin tones (controlled variety allowed):
- Use a 3-tone skin ramp that matches the player's complexion:
  highlight, midtone, shadow (max 3 tones).
- The ramp must remain warm (no gray/blue skin) and should be created by blending:
  #E8B4A8 + neutrals (#ffffff/#f8fafc/#f1f5f9) for lighter complexions,
  AND for darker complexions, allow deeper warm browns by blending:
  #E8B4A8 with slate darks (#334155/#1e293b/#0f172a) to reach richer tones.
- Avoid making all players the same peach; match the player's real-world complexion within this warm ramp.

Hair/outline: slate darks (#1e293b/#334155).

Jersey base: slate mid (#334155/#475569) with trim in #4A7FB8 and a tiny cyan highlight.

Shadows: clean, flat shapes (no airbrush), using #475569/#334155 tints.

Background (must match DraftGuru site feel):
- Base: soft vertical gradient from #ffffff (top) to #f8fafc (bottom).
- Add a simple geometric motif behind the head: a ring or concentric circles using #4A7FB8 at low opacity.
- Pep level 2: add ONE subtle "energy layer":
  EITHER a faint dot-matrix texture OR very light scanlines (3–6% opacity),
  with cyan used sparingly as a glow edge or small accent in the ring.

Pep constraints:
- Energetic but clean; do not become neon-heavy or busy.
- Cyan accent must be subtle and controlled.

Clothing:
- Generic jersey or athletic top with simple trim only
- No numbers, no logos, no team marks

Hard exclusions:
- No text of any kind (no nameplates, no typography)
- No watermarks, signatures, credits
- No team logos, league marks, branded uniforms
- No busy stadium/crowd backgrounds
- No orange/yellow dominant palettes
- No caricature exaggeration; natural proportions
- Avoid "idealized generic face" — preserve distinctive facial structure

Abstraction rules:
- Do NOT render like a realistic digital portrait or anime avatar.
- Use graphic poster abstraction: simplified facial planes and hard-edged shadow shapes.
- Limit tonal steps: skin max 3 tones, hair max 2 tones, jersey max 2 tones.
- No smooth gradients or airbrushed shading on the face; shadows must be flat shapes.
- Eyes: smaller irises, minimal highlights, no eyelashes; avoid "anime" eye styling.
- Mouth/teeth: simplified; no individually detailed teeth.

Print vibe (preferred):
- Slight screenprint feel with clean vector edges.
- Optional subtle halftone/dot-matrix ONLY in background; never on the face.

Style anchor: editorial sports poster illustration, screenprint / vector trading-card vibe, simplified planes (not anime, not realistic portrait)."""

# System prompt versions for tracking iterations
SYSTEM_PROMPT_VERSIONS = {
    "default": DEFAULT_SYSTEM_PROMPT,
    "v1": DEFAULT_SYSTEM_PROMPT,
}


class ImageGenerationService:
    """Handles player image generation via Gemini API + S3 storage.

    Designed for reuse from both CLI and future admin UI.
    """

    def __init__(self) -> None:
        self._client: Optional[genai.Client] = None

    @property
    def client(self) -> genai.Client:
        """Lazily initialize the Gemini client."""
        if self._client is None:
            if not settings.gemini_api_key:
                raise ValueError("GEMINI_API_KEY not configured")
            self._client = genai.Client(api_key=settings.gemini_api_key)
        return self._client

    def get_system_prompt(self, version: str = "default") -> str:
        """Return system prompt by version.

        Args:
            version: Prompt version identifier

        Returns:
            System prompt text
        """
        return SYSTEM_PROMPT_VERSIONS.get(version, DEFAULT_SYSTEM_PROMPT)

    def get_s3_key(self, player_id: int, slug: str, style: str) -> str:
        """Generate S3 key for a player image.

        Args:
            player_id: Player database ID
            slug: Player URL slug
            style: Image style (default, vector, etc.)

        Returns:
            S3 key path like 'players/123_cooper-flagg_default.png'
        """
        return f"players/{player_id}_{slug}_{style}.png"

    def build_player_prompt(
        self,
        player: PlayerMaster,
        likeness_description: Optional[str] = None,
    ) -> str:
        """Build the user prompt for a specific player.

        Args:
            player: Player database record
            likeness_description: Optional description from reference image

        Returns:
            Complete user prompt for image generation
        """
        prompt_parts = [f"Player: {player.display_name}"]
        prompt_parts.append(
            "Pose: chest-up, centered, straight-on. "
            "Facial expression: friendly, relaxed, confident"
        )

        if likeness_description:
            prompt_parts.append(
                f"Likeness locks (must preserve):\n{likeness_description}"
            )

        prompt_parts.append(
            """Background motif:
DraftGuru UI gradient (#ffffff → #f8fafc)
One low-opacity ring/concentric circles in #4A7FB8
Pep=2 energy layer: subtle dot-matrix texture + tiny cyan glow edge (#06b6d4) on the ring (very light)

Clothing:
Generic jersey, no logos/numbers. Trim in #4A7FB8 with a tiny cyan highlight."""
        )

        return "\n".join(prompt_parts)

    async def describe_reference_image(self, image_url: str) -> str:
        """Fetch image and generate likeness description via Gemini vision.

        Uses Gemini's vision capabilities to describe a player's distinctive
        facial features for use in image generation.

        Args:
            image_url: URL to reference image

        Returns:
            Text description of player's appearance
        """
        logger.info(f"Fetching and describing reference image: {image_url}")

        description_prompt = """Analyze this basketball player's face and describe their distinctive features for an illustrator. Focus on:

1. Hair: color, length, texture, style (braids, fade, afro, etc.), hairline shape
2. Brows: shape, thickness, arch
3. Eyes: shape, spacing, any distinctive characteristics
4. Nose: bridge width, nostril shape, overall size
5. Jaw/chin: square vs tapered, chin shape
6. Mouth: lip thickness, mouth width, typical expression
7. Skin tone: describe the complexion (light, medium, deep, etc.)
8. Facial hair: any beard, mustache, goatee details
9. Any other distinctive features (dimples, scars, moles, etc.)

Be specific and objective. This will help an AI illustrator capture their likeness accurately."""

        try:
            response = self.client.models.generate_content(
                model="gemini-2.0-flash",
                contents=[
                    types.Content(
                        role="user",
                        parts=[
                            types.Part.from_uri(
                                file_uri=image_url, mime_type="image/jpeg"
                            ),
                            types.Part.from_text(text=description_prompt),
                        ],
                    ),
                ],
            )
            description = response.text if response.text else ""
            logger.info(f"Generated likeness description: {len(description)} chars")
            return description
        except Exception as e:
            logger.error(f"Failed to describe reference image: {e}")
            raise

    async def generate_image(
        self,
        user_prompt: str,
        system_prompt: str,
        image_size: str = "1K",
    ) -> bytes:
        """Call Gemini API to generate an image.

        Args:
            user_prompt: Player-specific prompt
            system_prompt: System instructions for style
            image_size: Size setting ("512", "1K", "2K")

        Returns:
            Image data as bytes
        """
        logger.info(f"Generating image with size={image_size}")

        config = types.GenerateContentConfig(
            response_modalities=["IMAGE", "TEXT"],
            image_config=types.ImageConfig(image_size=image_size),
            system_instruction=[types.Part.from_text(text=system_prompt)],
        )

        contents = [
            types.Content(
                role="user",
                parts=[types.Part.from_text(text=user_prompt)],
            ),
        ]

        # Use streaming to handle large image responses
        image_data: Optional[bytes] = None
        for chunk in self.client.models.generate_content_stream(
            model="gemini-3-pro-image-preview",
            contents=contents,
            config=config,
        ):
            if (
                chunk.candidates is None
                or chunk.candidates[0].content is None
                or chunk.candidates[0].content.parts is None
            ):
                continue

            part = chunk.candidates[0].content.parts[0]
            if part.inline_data and part.inline_data.data:
                image_data = part.inline_data.data
                break

        if image_data is None:
            raise RuntimeError("No image data received from Gemini API")

        logger.info(f"Received image: {len(image_data)} bytes")
        return image_data

    async def generate_for_player(
        self,
        db: AsyncSession,
        player: PlayerMaster,
        snapshot: PlayerImageSnapshot,
        style: str = "default",
        fetch_likeness: bool = False,
        likeness_url: Optional[str] = None,
        image_size: Optional[str] = None,
    ) -> PlayerImageAsset:
        """Generate image for a single player.

        Creates the image via Gemini, uploads to S3, and saves asset record.

        Args:
            db: Database session
            player: Player to generate image for
            snapshot: Parent snapshot record
            style: Image style
            fetch_likeness: Whether to fetch and describe a reference image
            likeness_url: Explicit reference image URL (overrides player.reference_image_url)
            image_size: Override for image size

        Returns:
            Created PlayerImageAsset record
        """
        start_time = time.time()
        size = image_size or settings.image_gen_size

        # Determine reference URL
        ref_url = likeness_url or (
            player.reference_image_url if fetch_likeness else None
        )

        # Get likeness description if needed
        likeness_description: Optional[str] = None
        if ref_url:
            try:
                likeness_description = await self.describe_reference_image(ref_url)
            except Exception as e:
                logger.warning(f"Failed to get likeness for {player.display_name}: {e}")

        # Build prompt
        user_prompt = self.build_player_prompt(player, likeness_description)

        snapshot_id = snapshot.id
        player_id = player.id
        if snapshot_id is None or player_id is None:
            raise ValueError("snapshot.id and player.id are required")

        s3_key = self.get_s3_key(player_id, player.slug or str(player_id), style)
        public_url_for_audit = (
            s3_client.get_public_url(s3_key)
            if s3_client.use_local or settings.s3_bucket_name
            else ""
        )

        image_data: bytes | None = None
        error_message: str | None = None

        try:
            image_data = await self.generate_image(
                user_prompt=user_prompt,
                system_prompt=snapshot.system_prompt,
                image_size=size,
            )
        except Exception as exc:  # noqa: BLE001
            error_message = str(exc)

        if image_data is not None and error_message is None:
            try:
                public_url_for_audit = s3_client.upload(
                    s3_key,
                    image_data,
                    content_type="image/png",
                )
            except Exception as exc:  # noqa: BLE001
                error_message = str(exc)

        asset = PlayerImageAsset(
            snapshot_id=snapshot_id,
            player_id=player_id,
            s3_key=s3_key,
            s3_bucket=settings.s3_bucket_name,
            public_url=public_url_for_audit,
            file_size_bytes=len(image_data) if image_data is not None else None,
            user_prompt=user_prompt,
            likeness_description=likeness_description,
            used_likeness_ref=bool(ref_url),
            reference_image_url=ref_url,
            error_message=error_message,
            generated_at=datetime.utcnow(),
            generation_time_sec=time.time() - start_time,
        )
        db.add(asset)

        if error_message:
            logger.error(
                f"Failed to generate image for {player.display_name}: {error_message}"
            )
        else:
            logger.info(
                f"Generated image for {player.display_name}: "
                f"{len(image_data or b'')} bytes in {asset.generation_time_sec:.1f}s"
            )

        return asset


# Singleton instance for convenience
image_generation_service = ImageGenerationService()
