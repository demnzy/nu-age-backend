from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session
from sqlalchemy import or_, and_, func, asc, delete
from typing import List, Optional
from uuid import UUID
import uuid
import base64
from pydantic import BaseModel

import models
import schemas
from database import get_db
from services import auth
from services.bunny_service import upload_bytes_to_bunny

router = APIRouter(
    prefix="/playlists",
    tags=["Playlists"]
)

@router.post("/", response_model=schemas.PlaylistOut, status_code=status.HTTP_201_CREATED)
async def create_playlist(payload: schemas.PlaylistCreate, db: Session = Depends(get_db), user=Depends(auth.get_current_user)):
    if user.role != "Admin" and user.role != "owner":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have the permission to perform this operation"
        )

    playlist_data = payload.model_dump(exclude={"image_bytes", "image_filename"})
    playlist_data['creator_id'] = user.id

    playlist = models.Playlist(**playlist_data)
    db.add(playlist)
    db.commit()
    db.refresh(playlist)

    if payload.image_bytes and payload.image_filename:
        try:
            raw_image_bytes = base64.b64decode(payload.image_bytes)
            file_extension = payload.image_filename.split(".")[-1]
            safe_filename = f"thumbnail_{uuid.uuid4().hex}.{file_extension}"
            folder_path = f"playlists/{playlist.id}"
            
            cdn_url = await upload_bytes_to_bunny(raw_image_bytes, safe_filename, folder_path)
            
            playlist.image_url = cdn_url
            db.commit()
            db.refresh(playlist)
            
        except Exception as e:
            print(f"Warning: Playlist created, but image upload failed: {str(e)}")

    return playlist

@router.get("/", response_model=List[schemas.PlaylistOut])
def get_all_playlists(db: Session = Depends(get_db), user=Depends(auth.get_current_user)):
    results = (
        db.query(models.Playlist, models.Organisation.name)
        .join(models.Organisation, models.Organisation.id == models.Playlist.org_id)
        .filter(models.Playlist.is_public == True)
        .all()
    )

    return [
        {
            "id": playlist.id,
            "name": playlist.name,
            "description": playlist.description,
            "org_id": playlist.org_id,
            "Organisation": org_name,
            "image_url": playlist.image_url,
            "rating": playlist.rating,
            "is_public": playlist.is_public,
            "creator_id": playlist.creator_id,
            "created_at": playlist.created_at,
            "updated_at": playlist.updated_at
        }
        for playlist, org_name in results ]
@router.get("/orgs/{org_id}", response_model=List[schemas.PlaylistOut])
def get_org_playlists(org_id: UUID, db: Session = Depends(get_db), user=Depends(auth.get_current_user)):
    playlists = db.query(models.Playlist).filter(models.Playlist.org_id == org_id).all()
    return playlists

@router.get("/{playlist_id}", response_model=schemas.PlaylistOut)
def get_playlist(playlist_id: UUID, db: Session = Depends(get_db), user=Depends(auth.get_current_user)):
    playlist = db.query(models.Playlist).filter(models.Playlist.id == playlist_id).first()
    if not playlist:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Playlist not found")
    return playlist

@router.put("/{playlist_id}", response_model=schemas.PlaylistOut)
async def update_playlist(playlist_id: UUID, payload: schemas.PlaylistUpdate, db: Session = Depends(get_db), user=Depends(auth.get_current_user)):
    playlist = db.query(models.Playlist).filter(models.Playlist.id == playlist_id).first()
    if not playlist:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Playlist not found")

    if user.role != "Admin" and user.role != "owner":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")

    update_data = payload.model_dump(exclude_unset=True, exclude={"image_bytes", "image_filename"})
    for key, value in update_data.items():
        setattr(playlist, key, value)

    if payload.image_bytes and payload.image_filename:
        try:
            raw_image_bytes = base64.b64decode(payload.image_bytes)
            file_extension = payload.image_filename.split(".")[-1]
            safe_filename = f"thumbnail_{uuid.uuid4().hex}.{file_extension}"
            folder_path = f"playlists/{playlist.id}"
            
            cdn_url = await upload_bytes_to_bunny(raw_image_bytes, safe_filename, folder_path)
            playlist.image_url = cdn_url
        except Exception as e:
            print(f"Warning: image upload failed: {str(e)}")

    db.commit()
    db.refresh(playlist)
    return playlist

class CourseMappingPayload(BaseModel):
    course_ids: List[UUID]

@router.post("/{playlist_id}/courses", status_code=status.HTTP_201_CREATED)
def add_courses_to_playlist(playlist_id: UUID, payload: CourseMappingPayload, db: Session = Depends(get_db), user=Depends(auth.get_current_user)):
    playlist = db.query(models.Playlist).filter(models.Playlist.id == playlist_id).first()
    if not playlist:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Playlist not found")

    if user.role != "Admin" and user.role != "owner":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")

    max_order = db.query(func.max(models.PlaylistCourse.order_index)).filter(models.PlaylistCourse.playlist_id == playlist_id).scalar() or 0

    added = 0
    for cid in payload.course_ids:
        exists = db.query(models.PlaylistCourse).filter_by(playlist_id=playlist_id, course_id=cid).first()
        if not exists:
            max_order += 1
            new_mapping = models.PlaylistCourse(
                playlist_id=playlist_id,
                course_id=cid,
                order_index=max_order
            )
            db.add(new_mapping)
            added += 1

    db.commit()
    return {"message": f"Added {added} courses to playlist."}

class BulkPlaylistCoursesPayload(BaseModel):
    course_ids: List[UUID]

@router.post("/{playlist_id}/courses/bulk", status_code=status.HTTP_200_OK)
def save_bulk_playlist_courses(playlist_id: UUID, payload: BulkPlaylistCoursesPayload, db: Session = Depends(get_db), user=Depends(auth.get_current_user)):
    playlist = db.query(models.Playlist).filter(models.Playlist.id == playlist_id).first()
    if not playlist:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Playlist not found")

    if user.role != "Admin" and user.role != "owner":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")

    db.query(models.PlaylistCourse).filter(models.PlaylistCourse.playlist_id == playlist_id).delete(synchronize_session=False)
    db.flush()

    added = 0
    for idx, cid in enumerate(payload.course_ids):
        new_mapping = models.PlaylistCourse(
            playlist_id=playlist_id,
            course_id=cid,
            order_index=idx
        )
        db.add(new_mapping)
        added += 1

    db.commit()
    return {"message": f"Bulk saved {added} courses to playlist."}

@router.delete("/{playlist_id}/courses/{course_id}")
def remove_course_from_playlist(playlist_id: UUID, course_id: UUID, db: Session = Depends(get_db), user=Depends(auth.get_current_user)):
    if user.role != "Admin" and user.role != "owner":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")

    mapping = db.query(models.PlaylistCourse).filter_by(playlist_id=playlist_id, course_id=course_id).first()
    if not mapping:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Course not in playlist")

    db.delete(mapping)
    db.commit()
    return {"message": "Course removed from playlist"}

class ReorderPayload(BaseModel):
    course_id: UUID
    direction: str

@router.put("/{playlist_id}/courses/reorder")
def reorder_course(playlist_id: UUID, payload: ReorderPayload, db: Session = Depends(get_db), user=Depends(auth.get_current_user)):
    if user.role != "Admin" and user.role != "owner":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")

    target = db.query(models.PlaylistCourse).filter_by(playlist_id=playlist_id, course_id=payload.course_id).first()
    if not target:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Course not found in playlist")

    all_courses = db.query(models.PlaylistCourse).filter_by(playlist_id=playlist_id).order_by(models.PlaylistCourse.order_index).all()
    
    idx = all_courses.index(target)
    
    if payload.direction == "up" and idx > 0:
        prev = all_courses[idx-1]
        target.order_index, prev.order_index = prev.order_index, target.order_index
    elif payload.direction == "down" and idx < len(all_courses) - 1:
        next_course = all_courses[idx+1]
        target.order_index, next_course.order_index = next_course.order_index, target.order_index
        
    db.commit()
    return {"message": "Order updated"}

@router.post("/{playlist_id}/enroll")
def enroll_in_playlist(playlist_id: UUID, db: Session = Depends(get_db), user=Depends(auth.get_current_user)):
    playlist = db.query(models.Playlist).filter_by(id=playlist_id).first()
    if not playlist:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Playlist not found")

    penroll = db.query(models.PlaylistEnrollment).filter_by(playlist_id=playlist_id, student_id=user.id).first()
    if not penroll:
        penroll = models.PlaylistEnrollment(playlist_id=playlist_id, student_id=user.id)
        db.add(penroll)
    
    courses = db.query(models.PlaylistCourse).filter_by(playlist_id=playlist_id).all()
    for pc in courses:
        exists = db.query(models.Enrollment).filter_by(course_id=pc.course_id, student_id=user.id).first()
        if not exists:
            db.add(models.Enrollment(course_id=pc.course_id, student_id=user.id))
            
    db.commit()
    return {"message": "Successfully enrolled in playlist and its courses."}

@router.get("/{playlist_id}/analytics")
def playlist_analytics(playlist_id: UUID, db: Session = Depends(get_db), user=Depends(auth.get_current_user)):
    if user.role != "Admin" and user.role != "owner":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
        
    enrollments = db.query(models.PlaylistEnrollment).filter_by(playlist_id=playlist_id).all()
    
    data = []
    for en in enrollments:
        data.append({
            "student_id": en.student_id,
            "student_name": f"{en.student.first_name} {en.student.last_name}",
            "enrolled_at": en.enrolled_at,
            "progress": en.progress,
            "completed_at": en.completed_at
        })
    return data
