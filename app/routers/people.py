"""Person (Contact) endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db

router = APIRouter(prefix="/api/people", tags=["people"])


@router.post("", response_model=schemas.PersonRead, status_code=201)
def create_person(payload: schemas.PersonCreate, db: Session = Depends(get_db)):
    if not db.get(models.Company, payload.company_id):
        raise HTTPException(400, "company_id does not exist")
    if payload.application_id and not db.get(
        models.JobApplication, payload.application_id
    ):
        raise HTTPException(400, "application_id does not exist")
    person = models.Person(**payload.model_dump())
    db.add(person)
    db.commit()
    db.refresh(person)
    return person


@router.get("", response_model=list[schemas.PersonRead])
def list_people(
    champions_only: bool = False, db: Session = Depends(get_db)
):
    q = db.query(models.Person)
    if champions_only:
        q = q.filter(models.Person.is_champion == 1)
    return q.order_by(models.Person.name).all()


@router.get("/{person_id}", response_model=schemas.PersonRead)
def get_person(person_id: int, db: Session = Depends(get_db)):
    person = db.get(models.Person, person_id)
    if not person:
        raise HTTPException(404, "Person not found")
    return person
