
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Bot Configuration
API_ID = 25800893
API_HASH = "6ccc441e2cbd1ef33ee523fd2349758b"
BOT_TOKEN = "8114598126:AAHKYsHwJEIB66LbCAHRyBYuPGa5SgxCWjA"

# Admin Configuration
ADMINS = [7969019802, 6403142441]

# Additional IDs
ADDITIONAL_IDS = [6403142441]

# Remove duplicates
ADMINS = list(set(ADMINS + ADDITIONAL_IDS))

PAID_USERS = []

# Default Settings
DEFAULT_UPLOAD_MODE = "video"
DEFAULT_SPLIT_SETTING = True
DEFAULT_THUMBNAIL_GENERATION = True
DEFAULT_CAPTION_ENABLED = False
DEFAULT_GENERATE_SCREENSHOTS = False
DEFAULT_SAMPLE_VIDEO = False

# YT-DLP Default Settings
DEFAULT_FORMAT = "bestvideo+bestaudio"

# Contact Information
CONTACT_ADMIN = "@Shitijbro for paid subscription"

# File Size Limits
MAX_FILE_SIZE = int(1.75 * (1024 ** 3))  # 1.75GB

# Free Users Tasks
TASKS = 20

# Temporary Download Path
DOWNLOAD_PATH = "./downloads/"
