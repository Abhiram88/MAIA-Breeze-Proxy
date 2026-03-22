# Environment Setup

This application uses a `.env` file to store sensitive credentials instead of Google Secret Manager.

## Setup Instructions

1. **Copy the example file:**
   ```bash
   cd breeze-proxy
   cp .env.example .env
   ```

2. **Edit `.env` with your credentials:**
   ```bash
   nano .env  # or use your preferred editor
   ```

   **Alternative: YAML config file**
   You can also create an `env.yaml` file in the `breeze-proxy/` directory:
   ```yaml
   BREEZE_API_KEY: "your_key"
   BREEZE_API_SECRET: "your_secret"
   GEMINI_API_KEY: "your_gemini_key"
   # ... other keys
   ```
   The app checks environment variables first, then falls back to `env.yaml`.

3. **Required Environment Variables:**
   - `BREEZE_API_KEY` - #8#
   - `BREEZE_API_SECRET` - ####
   - `BREEZE_PROXY_ADMIN_KEY` - ###$
   - `GEMINI_API_KEY` - #
   - `SUPABASE_KEY` - ##
   - `SUPABASE_URL` - ht##
   - `STOCKINSIGHTS_API_URL` - ##ment
   - `STOCKINSIGHTS_API_KEY` - zp#########

4. **Security Notes:**
   - ✅ The `.env` file is included in `.gitignore` and will NOT be committed
   - ✅ Never share your `.env` file or commit it to version control
   - ✅ Use `.env.example` as a template for others without exposing secrets

## Running the Application

After setting up your `.env` file:

```bash
cd breeze-proxy
pip install -r requirements.txt
python breeze_proxy_app.py
```

The application will automatically load environment variables from the `.env` file on startup.

## Deployment

For production deployments (Cloud Run, Docker, etc.), you can:
- Set environment variables directly in the deployment configuration
- Mount the `.env` file as a secret volume (not recommended for cloud)
- Use the platform's secret management (but configure as environment variables)

The application will work with environment variables from any source - it doesn't require the `.env` file in production if variables are set through other means.
