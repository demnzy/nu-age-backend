import httpx
from fastapi import UploadFile, HTTPException
from database import Settings

settings = Settings()

BUNNY_STORAGE_KEY = settings.BUNNY_STORAGE_KEY
STORAGE_ZONE_NAME = settings.STORAGE_ZONE_NAME
BUNNY_REGION_URL = settings.BUNNY_REGION_URL
PULL_ZONE_URL = settings.PULL_ZONE_URL
STREAM_API_KEY = settings.STREAM_API_KEY
STREAM_LIBRARY_ID= settings.STREAM_LIBRARY_ID
PRIVATE_STORAGE_KEY = settings.PRIVATE_STORAGE_KEY
PRIVATE_STORAGE_ZONE = settings.PRIVATE_STORAGE_ZONE
STREAM_CDN_HOSTNAME = settings.STREAM_CDN_HOSTNAME
async def upload_bytes_to_bunny(raw_bytes: bytes, filename: str, folder_path: str = "") -> str:
    """
    Uploads raw bytes to Bunny.net and returns the public Nu-age CDN URL.
    """
    # 1. Construct the API Upload URL using the explicitly passed filename
    upload_url = f"https://{BUNNY_REGION_URL}/{STORAGE_ZONE_NAME}/{folder_path}/{filename}"
    
    # 2. Set the required headers
    headers = {
        "AccessKey": BUNNY_STORAGE_KEY,
        "Content-Type": "application/octet-stream",
        "Accept": "application/json"
    }

    # 3. Push the raw bytes directly to Bunny.net
    async with httpx.AsyncClient(timeout=60.0) as client:
        # We pass raw_bytes directly to the content parameter
        response = await client.put(upload_url, content=raw_bytes, headers=headers)
        
        if response.status_code != 201:
            raise HTTPException(
                status_code=500, 
                detail=f"CDN Upload Failed. Bunny returned: {response.text}"
            )

    # 4. Return the playable/viewable CDN URL
    return f"{PULL_ZONE_URL}/{folder_path}/{filename}"

# Assuming you loaded these via dotenv

async def upload_video_to_stream(video_bytes: bytes, title: str) -> str:
    """
    Uploads a video to Bunny Stream and returns the direct HLS (.m3u8) URL
    required for native Flet video players.
    """
    # 1. Create the Video Placeholder
    create_url = f"https://video.bunnycdn.com/library/{STREAM_LIBRARY_ID}/videos"
    headers = {
        "AccessKey": STREAM_API_KEY,
        "Accept": "application/json",
        "Content-Type": "application/json"
    }
    
    async with httpx.AsyncClient() as client:
        create_res = await client.post(create_url, json={"title": title}, headers=headers)
        if create_res.status_code != 200:
            raise HTTPException(status_code=500, detail="Failed to create video placeholder.")
            
        video_guid = create_res.json()["guid"]

    # 2. Upload the raw video bytes to that specific GUID
    upload_url = f"https://video.bunnycdn.com/library/{STREAM_LIBRARY_ID}/videos/{video_guid}"
    upload_headers = { "AccessKey": STREAM_API_KEY } 

    async with httpx.AsyncClient(timeout=300.0) as client: 
        upload_res = await client.put(upload_url, content=video_bytes, headers=upload_headers)
        if upload_res.status_code != 200:
            raise HTTPException(status_code=500, detail="Failed to upload video bytes.")

    # 3. Return the fully playable HLS (.m3u8) playlist link!
    return f"https://{STREAM_CDN_HOSTNAME}/{video_guid}/playlist.m3u8"

async def upload_audio_to_bunny(audio_bytes: bytes, filename: str, folder_path: str = "") -> str:
    """
    Uploads premium audio bytes to the private Bunny.net storage zone.
    Returns the internal storage path, NOT a public URL.
    """
    # 1. Construct the API Upload URL targeting the PRIVATE zone
    upload_url = f"https://{BUNNY_REGION_URL}/{PRIVATE_STORAGE_ZONE}/{folder_path}/{filename}"
    
    # 2. Set the required headers using the private storage password
    headers = {
        "AccessKey": PRIVATE_STORAGE_KEY,
        "Content-Type": "application/octet-stream", # Works universally for mp3, wav, etc.
        "Accept": "application/json"
    }

    # 3. Push the raw bytes to Bunny.net
    # CRITICAL: We set timeout=300.0 (5 minutes) so it doesn't crash on large 30MB+ audio files
    async with httpx.AsyncClient(timeout=300.0) as client:
        response = await client.put(upload_url, content=audio_bytes, headers=headers)
        
        if response.status_code != 201:
            raise HTTPException(
                status_code=500, 
                detail=f"Audio Upload Failed. Bunny returned: {response.text}"
            )

    # 4. Return ONLY the path so we can save it in the database and sign it later
    # Example return: "/courses/123/audio/lesson_1.mp3"
    return f"/{folder_path}/{filename}"