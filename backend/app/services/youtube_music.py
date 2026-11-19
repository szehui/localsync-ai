"""YouTube Music service for parsing playlists using yt-dlp."""
import re
import subprocess
import json
import logging
from typing import Dict, List, Any, Optional

logger = logging.getLogger(__name__)

# Trailing edition/version noise that carries no identity for search or scoring.
# "Hotel California (2013 Remaster)" and "Hotel California" are the same song to
# Navidrome's search index only if the parenthetical is gone.
_VERSION_NOISE_RE = re.compile(
    r"""\s*[\(\[]\s*[^\)\]]*?
        (remaster|remastered|version|edit|mix|mono|stereo|deluxe|
         bonus|explicit|clean|anniversary|digital|single|radio)
        [^\)\]]*?[\)\]]\s*$""",
    re.IGNORECASE | re.VERBOSE,
)

# YouTube channel names are not artist names. These suffixes are added by labels
# and auto-generated "- Topic" channels, and they wreck artist similarity scoring
# ("Queen Official" vs "Queen" only scores 0.63).
_TOPIC_SUFFIX_RE = re.compile(r"\s*[-\u2013\u2014]\s*topic\s*$", re.IGNORECASE)
_CHANNEL_SUFFIX_RE = re.compile(
    r"\s*\b(vevo|official|tv|channel)\b\.?\s*$", re.IGNORECASE
)
# Suffixes glued to the name with no separator, e.g. "HouseOfPainTV".
_GLUED_SUFFIX_RE = re.compile(r"(?<=[a-z])(vevo|official|tv|channel)\s*$", re.IGNORECASE)


def clean_track_title(title: str) -> str:
    """Strip trailing edition/version noise from a track title.

    Used for both the YouTube title and the library title when searching and
    scoring, so "(2013 Remaster)" on one side and "(Album Version)" on the other
    don't prevent a match. Falls back to the raw title if cleaning empties it.
    """
    if not title:
        return ""
    cleaned = title.strip()
    for _ in range(3):  # e.g. "... (Album Version) [Remastered]"
        stripped = _VERSION_NOISE_RE.sub("", cleaned).strip()
        if stripped == cleaned:
            break
        cleaned = stripped
    cleaned = cleaned.strip(" -\u2013\u2014")
    return cleaned or title.strip()


def clean_artist_name(name: str) -> str:
    """Strip YouTube channel noise from an artist name.

    Handles auto-generated "- Topic" channels plus label channel suffixes such as
    "Official", "TV", "VEVO", "Channel", and the glued forms ("HouseOfPainTV").
    Conservative by design: it only removes a trailing "Band" when a real name
    remains, so "The Band" survives intact.
    """
    if not name:
        return ""
    cleaned = _TOPIC_SUFFIX_RE.sub("", name.strip())
    for _ in range(3):
        before = cleaned
        cleaned = _CHANNEL_SUFFIX_RE.sub("", cleaned)
        cleaned = _GLUED_SUFFIX_RE.sub("", cleaned)
        cleaned = cleaned.strip(" -\u2013\u2014")
        if cleaned == before:
            break
    band = re.search(r"\s+band\s*$", cleaned, re.IGNORECASE)
    if band and len(cleaned[: band.start()].strip()) >= 4:
        cleaned = cleaned[: band.start()].strip()
    return cleaned or name.strip()



def _parse_artist_title(title: str) -> tuple[str, str]:
    """Try to extract artist and track from a YouTube video title.
    
    Common formats on YouTube Music:
        "Artist – Title (Official Video)"
        "Artist - Title (Official Music Video)"
        "Artist - Title"
        "Title (feat. Artist)"
    """
    t = title.strip()
    
    # Remove common suffixes in parentheses/brackets
    for pattern in [
        r'\s*\(Official Video Remastered\)',
        r'\s*\(Official Video\)',
        r'\s*\(Official Music Video\)',
        r'\s*\(Music Video\)',
        r'\s*\(Lyric Video\)',
        r'\s*\(Lyrics\)',
        r'\s*\(Audio\)',
        r'\s*\(Official Audio\)',
        r'\s*\(Live\)',
        r'\s*\(Remastered\)',
        r'\s*\(Remaster\)',
        r'\s*\(Explicit\)',
        r'\s*\(Clean\)',
        r'\s*\[Official Video\]',
        r'\s*\[Lyrics\]',
        r'\s*\[Official Music Video\]',
        r'\s*\[Audio\]',
        r'\s*\(Official Visualizer\)',
        r'\s*\(Visualizer\)',
    ]:
        t = re.sub(pattern, '', t, flags=re.IGNORECASE)
    
    # "Artist – Title" or "Artist - Title" pattern (most common on YT Music)
    # Use both dash types
    for sep in [' – ', ' - ', ' — ']:
        if sep in t:
            parts = t.split(sep, 1)
            return parts[0].strip(), parts[1].strip()
    
    # "Artist: Title" pattern (less common)
    if ': ' in t:
        parts = t.split(': ', 1)
        if len(parts[0]) < 60:  # reasonable artist name length
            return parts[0].strip(), parts[1].strip()
    
    # Fallback: no artist extracted, use entire title as track name
    return '', t.strip()


class YouTubeMusicService:
    """Service for parsing YouTube Music playlists using yt-dlp."""

    def _extract_playlist_id(self, url: str) -> str:
        """Extract playlist ID from various YouTube Music URL formats."""
        patterns = [
            r"https?://music\.youtube\.com/playlist\?list=([a-zA-Z0-9_-]+)",
            r"https?://(?:www\.)?youtube\.com/playlist\?list=([a-zA-Z0-9_-]+)",
            r"https?://youtu\.be/playlist\?list=([a-zA-Z0-9_-]+)",
            r"https?://music\.youtube\.com/browse/([a-zA-Z0-9_-]+)",
        ]

        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)

        # If it looks like a raw playlist ID
        if re.match(r"^[a-zA-Z0-9_-]+$", url):
            return url

        raise ValueError(f"Could not extract playlist ID from URL: {url}")

    async def get_playlist_tracks(self, url: str) -> Dict[str, Any]:
        """
        Get track information from a YouTube Music playlist using yt-dlp.

        Uses --flat-playlist for fast metadata extraction (no audio download).
        Parses video titles to extract artist/track info.

        Args:
            url: YouTube Music playlist URL

        Returns:
            Dictionary with playlist title and tracks
        """
        playlist_id = self._extract_playlist_id(url)

        # Build the canonical YouTube URL
        playlist_url = f"https://www.youtube.com/playlist?list={playlist_id}"

        # yt-dlp flat playlist: gets video titles + IDs without downloading
        cmd = [
            'yt-dlp', '--dump-json', '--flat-playlist',
            '--no-warnings', '--no-check-certificates',
            playlist_url,
        ]

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120,
            )

            if result.returncode != 0:
                stderr = result.stderr.strip()
                if '404' in stderr or 'not found' in stderr.lower():
                    raise ValueError(
                        f"Playlist not found. Check the URL is correct "
                        f"and the playlist is publicly accessible."
                    )
                raise ValueError(f"Failed to fetch playlist: {stderr[:200]}")

            # Parse output: ALL lines are entries (videos). Playlist-level
            # metadata (title, count) is embedded in each entry as
            # playlist_title, playlist, playlist_count, etc.
            lines = result.stdout.strip().split('\n')
            if not lines or not lines[0].strip():
                raise ValueError(
                    f"No data returned for playlist ID: {playlist_id}. "
                    "The playlist may be empty or private."
                )

            # Parse all lines as entries
            entries = []
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                entries.append(json.loads(line))

            # Extract playlist title from the first entry
            playlist_title = (
                entries[0].get('playlist_title')
                or entries[0].get('playlist')
                or 'Unknown Playlist'
            )

            tracks = []
            for entry in entries:
                video_title = entry.get('title', '')
                video_id = entry.get('id', '')
                channel = entry.get('channel', '') or entry.get('uploader', '') or ''
                duration = entry.get('duration') or 0

                # Parse artist + track from the video title
                artist, track_title = _parse_artist_title(video_title)

                # If no artist parsed from title, use the channel name
                if not artist and channel:
                    # Strip YouTube auto-generated "- Topic" suffix
                    # e.g. "BTS - Topic" → "BTS"
                    artist = re.sub(r"\s*-\s*Topic$", "", channel, flags=re.IGNORECASE)
                    artist = artist.strip()

                # Channel names are not artist names: "Queen Official",
                # "HouseOfPainTV" and "SirMixAlotChannel" would otherwise be sent
                # to Lidarr as lookup terms and would wreck similarity scoring.
                artist = clean_artist_name(artist)

                tracks.append({
                    'title': track_title,
                    'artist': artist,
                    'album': '',  # Not available from flat playlist
                    'duration': int(duration) if duration else 0,
                    'youtube_id': video_id,
                })

            return {
                'title': playlist_title,
                'track_count': len(tracks),
                'tracks': tracks,
            }

        except subprocess.TimeoutExpired:
            raise ValueError(
                "Playlist parsing timed out. "
                "The playlist may be very large; try a smaller one."
            )
        except json.JSONDecodeError as e:
            raise ValueError(f"Failed to parse playlist data: {e}")


# Global service instance
youtube_music_service = YouTubeMusicService()
