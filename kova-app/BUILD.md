# Kova Finance Android App — Build Instructions

## Prerequisites (install once)

1. **Node.js** — https://nodejs.org  (choose LTS)
2. **Android Studio** — https://developer.android.com/studio
   - During install, accept all default SDK components

## Build the APK (run once, or after any change to www/)

Open a terminal in this folder (`kova-app/`) and run:

```
npm install
npx cap add android
npx cap sync
npx cap open android
```

This opens Android Studio. Then:
- Wait for Gradle sync to finish (bottom bar progress)
- Menu → Build → Build Bundle(s) / APK(s) → Build APK(s)
- When done, click "locate" in the notification → copy the .apk to your phone and install it

> Enable "Install from unknown sources" on your phone if prompted.

## After any change to www/index.html

```
npx cap sync
```
Then rebuild the APK in Android Studio.

## Daily use

1. Start the server on your PC:
   `python "C:\Users\anton\Desktop\Kova Finance\webapp\run.py"`

2. Start ngrok:
   `ngrok http 8080`

3. Open the Kova Finance app on your phone → paste the ngrok URL → Ligar
