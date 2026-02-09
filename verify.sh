#!/bin/bash

# Color codes
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}Verifying Project Setup${NC}"
echo -e "${BLUE}=========================${NC}\n"

ERRORS=0
WARNINGS=0

# Function to check if file exists and is not empty
check_file() {
    local file=$1
    local critical=$2
    
    if [ ! -f "$file" ]; then
        if [ "$critical" = "critical" ]; then
            echo -e "${RED}MISSING (Critical): $file${NC}"
            ((ERRORS++))
        else
            echo -e "${YELLOW}MISSING (Optional): $file${NC}"
            ((WARNINGS++))
        fi
        return 1
    elif [ ! -s "$file" ]; then
        echo -e "${YELLOW}EMPTY: $file${NC}"
        ((WARNINGS++))
        return 1
    else
        echo -e "${GREEN}$file${NC}"
        return 0
    fi
}

# Check critical files
echo -e "${BLUE}Checking Configuration Files...${NC}"
check_file "requirements.txt" "critical"
check_file ".env" "critical"
check_file ".env.example" "optional"
check_file "input-template.json"
check_file ".gitignore" "optional"
check_file "start.sh" "critical"
echo ""

# Check core application files
echo -e "${BLUE}Checking Core Application Files...${NC}"
check_file "main.py" "critical"
check_file "bots/__init__.py" "critical"
check_file "bots/google_flights_bot.py" "critical"
check_file "bots/myidtravel_bot.py" "critical"
check_file "helpers/legacy_bots/stafftraveler_bot.py" "optional"
check_file "helpers/legacy_bots/stafftraveler_bot_2.py" "optional"
check_file "helpers/legacy_bots/google_flights_bot.py" "optional"
check_file "helpers/legacy_bots/google_flights_bot_2.py" "optional"

check_file "index.html" "critical"
check_file "static/app.js" "critical"
check_file "static/style.css" "optional"

# Check if scripts are executable
echo -e "${BLUE}Checking Script Permissions...${NC}"

if [ -f "setup.sh" ]; then
    if [ -x "setup.sh" ]; then
        echo -e "${GREEN}setup.sh is executable${NC}"
    else
        echo -e "${YELLOW}setup.sh is not executable (run: chmod +x setup.sh)${NC}"
        ((WARNINGS++))
    fi
fi

if [ -f "start.sh" ]; then
    if [ -x "start.sh" ]; then
        echo -e "${GREEN}start.sh is executable${NC}"
    else
        echo -e "${RED}start.sh is not executable (run: chmod +x start.sh)${NC}"
        ((ERRORS++))
    fi
fi
echo ""

# Check environment directory
if [ -d ".venv" ]; then
    echo -e "${GREEN}✅ .venv/ directory exists${NC}"
else
    echo -e "${YELLOW}⚠️  .venv/ directory missing (run: python3 -m venv .venv)${NC}"
    ((WARNINGS++))
fi
echo ""

# Check .env configuration
if [ -f ".env" ]; then
    echo -e "${BLUE}Checking .env Configuration...${NC}"

    # Check required variables
    source .env 2>/dev/null

    # MyIDTravel Account
    [ -z "$UAL_USERNAME" ] && echo -e "${RED}UAL_USERNAME not set in .env${NC}" && ((ERRORS++))
    [ -z "$UAL_PASSWORD" ] && echo -e "${RED}UAL_PASSWORD not set in .env${NC}" && ((ERRORS++))

    # Stafftraveler Account
    [ -z "$ST_USERNAME" ] && echo -e "${RED}ST_USERNAME not set in .env${NC}" && ((ERRORS++))
    [ -z "$ST_PASSWORD" ] && echo -e "${RED}ST_PASSWORD not set in .env${NC}" && ((ERRORS++))

    if [ $ERRORS -eq 0 ]; then
        echo -e "${GREEN}✅ .env configuration looks good${NC}"
    fi
    echo ""
fi

# Summary
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BLUE}Summary${NC}"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

if [ $ERRORS -eq 0 ] && [ $WARNINGS -eq 0 ]; then
    echo -e "${GREEN}Everything is set up correctly.${NC}"
    echo -e "${GREEN}You can now run: ./start.sh${NC}"
    exit 0

elif [ $ERRORS -eq 0 ]; then
    echo -e "${YELLOW}Setup complete with $WARNINGS warning(s)${NC}"
    echo -e "${YELLOW}You can proceed, but review the warnings above.${NC}"
    echo -e "${GREEN}Run: ./start.sh${NC}"
    exit 0

else
    echo -e "${RED}Setup incomplete: $ERRORS error(s), $WARNINGS warning(s)${NC}"
    echo -e "${RED}Please fix the errors above before starting the application.${NC}"
    echo ""
    
    echo -e "${BLUE}Common fixes:${NC}"
    echo "1. Run: chmod +x setup.sh start.sh"
    echo "2. Create .env file from .env.example"
    echo "3. Install dependencies: pip install -r requirements.txt"
    echo "4. Check database connectivity"
    exit 1
fi
