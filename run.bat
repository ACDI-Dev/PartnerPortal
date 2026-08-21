@echo off
echo Starting Reseller Portal...

set FUSIONAUTH_CLIENT_ID=10ec4e31-10a5-417a-92e3-24b886e1c750
set FUSIONAUTH_CLIENT_SECRET=KVaWxgM5fTCVZrUhg4_wUkkmTWrD5xHQdj6Ssd6bRes
set FUSIONAUTH_URL=https://acdi.fusionauth.io/
set FLASK_SECRET_KEY=super-secret-key-for-local

python app.py

:: The pause command keeps the command prompt window open if the app crashes
pause