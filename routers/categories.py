from fastapi import *
from fastapi.security.oauth2 import OAuth2PasswordRequestForm
from schemas import *
from database import get_db
from sqlalchemy.orm import Session
import models
from services import utils, auth
from typing import List
import schemas

router = APIRouter(prefix="/categories")
def _require_category_admin(user):
    """Allows legacy Admin role or designated Platform Super Admins to manage categories."""
    role_str = str(getattr(user, "role", "")).upper()
    if getattr(user, "role", None) == "Admin" or "ADMIN" in role_str:
        return
    try:
        from routers.platform_admin import get_configured_platform_admins
        admins = get_configured_platform_admins()
        if user.username.lower() in admins or (user.email and user.email.lower() in admins):
            return
    except Exception:
        pass
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You dont have the permission to perform this action")


@router.post('/create', status_code=status.HTTP_201_CREATED)
def create_category(category:CategoryBase, db:Session = Depends(get_db), user = Depends(auth.get_current_user)):
    _require_category_admin(user)
    if db.query(models.Category).filter(models.Category.name== category.name).first():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Category with this name already exists")
    category= models.Category(**category.model_dump())
    db.add(category)
    db.commit()
    db.refresh(category)
    return category

@router.get('', response_model= List[CategoryOut])
def get_all_categories(name: str | None = Query(None, description= "Search Category by name"),db:Session = Depends(get_db), user = Depends(auth.get_current_user)):
    categories = db.query(models.Category).all()
    if name:
        categories = db.query(models.Category).filter(models.Category.name.ilike(f'%{name}%')).all()
    return categories

@router.patch('/update', response_model= CategoryBase)
def update_category(id:int, category:CategoryUpdate, db:Session = Depends(get_db), user = Depends(auth.get_current_user)):
    _require_category_admin(user)
    category_in_db = db.query(models.Category).filter(models.Category.id==id).first()
    if not category_in_db:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Category not found")
    if category.name:
        category_in_db.name = category.name
    if category.description:
        category_in_db.description = category.description
    db.commit()
    db.refresh(category_in_db)
    return category_in_db

@router.delete('/delete', status_code=status.HTTP_204_NO_CONTENT)
def delete_category(id:str, user= Depends(auth.get_current_user), db: Session = Depends(get_db)): 
    _require_category_admin(user)
    category = db.query(models.Category).filter(models.Category.id == id).first()
    if not category:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail= "Category Not found")
    print(user.first_name)
    db.delete(category)
    db.commit()