from aiogram import Router

from handlers.ai_tutor import router as ai_tutor_router
from handlers.assignments import router as assignments_router
from handlers.notes import router as notes_router
from handlers.reminders import router as reminders_router
from handlers.resources import router as resources_router
from handlers.schedule import router as schedule_router
from handlers.start import router as start_router


def setup_routers() -> Router:
    root_router = Router()
    root_router.include_router(start_router)
    root_router.include_router(schedule_router)
    root_router.include_router(assignments_router)
    root_router.include_router(resources_router)
    root_router.include_router(notes_router)
    root_router.include_router(reminders_router)
    root_router.include_router(ai_tutor_router)
    return root_router
