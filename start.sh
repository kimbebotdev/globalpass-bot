#!/bin/bash

# Color codes
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Load environment variables
if [ -f .env ]; then
    set -a
    source .env
    set +a
else
    echo -e "${RED}.env file not found!${NC}"
    echo "Please create a .env file based on .env.example"
    exit 1
fi

echo -e "${BLUE}Starting Globalpass Bot Application${NC}"
echo -e "${BLUE}================================${NC}\n"

# Check environment
echo -e "${GREEN}Environment:${NC} $APP_ENV"
echo -e "${GREEN}Port:${NC} $API_PORT"
echo ""

# Check if virtual environment exists
if [ ! -d ".venv" ]; then
    echo -e "${YELLOW}Virtual environment not found. Creating one...${NC}"
    python3 -m venv .venv
    echo -e "${GREEN}Virtual environment created${NC}\n"
fi

# Activate virtual environment
source .venv/bin/activate

# Install/update dependencies
echo -e "${BLUE}Checking dependencies...${NC}"
pip install -q -r requirements.txt
echo -e "${GREEN}Dependencies installed${NC}\n"

LOG_LEVEL_LOWER=${LOG_LEVEL:-INFO}
LOG_LEVEL_LOWER=$(echo "$LOG_LEVEL_LOWER" | tr '[:upper:]' '[:lower:]')

# Start the application based on environment
if [ "$APP_ENV" = "production" ]; then
    echo -e "${BLUE}Starting in PRODUCTION mode${NC}"
    echo -e "${YELLOW}Using $WORKERS workers${NC}\n"
    
    uvicorn app.main:app \
        --host ${API_HOST:-0.0.0.0} \
        --port ${API_PORT:-8000} \
        --workers ${WORKERS:-4} \
        --timeout-keep-alive 300 \
        --log-level $LOG_LEVEL_LOWER
else
    echo -e "${BLUE}Starting in DEVELOPMENT mode${NC}"
    echo -e "${YELLOW}Hot reload enabled${NC}\n"
    
    uvicorn app.main:app \
        --host ${API_HOST:-0.0.0.0} \
        --port ${API_PORT:-8000} \
        --reload \
        --log-level $LOG_LEVEL_LOWER
fi
