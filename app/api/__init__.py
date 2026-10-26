from app.api.academics import bp as academics_bp
from app.api.admin import bp as admin_bp
from app.api.auth import bp as auth_bp
from app.api.dashboards import bp as dashboards_bp
from app.api.data_admin import bp as data_admin_bp
from app.api.documents import bp as documents_bp
from app.api.institutions import bp as institutions_bp
from app.api.interviews import bp as interviews_bp
from app.api.letters import bp as letters_bp
from app.api.migration import bp as migration_bp
from app.api.notifications import bp as notifications_bp
from app.api.onboarding import bp as onboarding_bp
from app.api.search import bp as search_bp
from app.api.staff import bp as staff_bp
from app.api.students import bp as students_bp
from app.api.support import bp as support_bp
from app.api.vacancies import bp as vacancies_bp

BLUEPRINTS = [auth_bp, onboarding_bp, staff_bp, institutions_bp, academics_bp, vacancies_bp, students_bp,
              documents_bp, migration_bp, interviews_bp, letters_bp, notifications_bp, search_bp,
              dashboards_bp, support_bp, admin_bp, data_admin_bp]
