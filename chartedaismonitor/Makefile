# Run MPA import (requires: docker compose up -d, then)
import-mpas:
	docker compose exec backend python scripts/import_mpas.py

# Same using local Python (requires DATABASE_URL and deps installed in backend/)
import-mpas-local:
	cd backend && python scripts/import_mpas.py
