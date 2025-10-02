# Email Scheduler API Documentation

## Overview

This API allows you to manually trigger the email scheduler from your React frontend. The scheduler runs the same logic that GitHub Actions executes every 30 minutes.

## API Endpoints

### 1. Trigger Scheduler

**POST** `/api/trigger-scheduler`

Manually starts the email scheduler for all users.

**Headers:**
- `Content-Type: application/json`
- `X-API-Key: your-api-key` (optional, for authentication)

**Response:**
```json
{
  "success": true,
  "message": "Scheduler started successfully",
  "status": "running",
  "started_at": "2024-01-15T10:30:00.000Z"
}
```

**Error Response:**
```json
{
  "success": false,
  "error": "Scheduler is already running",
  "status": {
    "running": true,
    "last_run": "2024-01-15T10:25:00.000Z",
    "last_result": {...}
  }
}
```

### 2. Check Scheduler Status

**GET** `/api/scheduler-status`

Returns the current status of the scheduler.

**Response:**
```json
{
  "running": false,
  "last_run": "2024-01-15T10:30:00.000Z",
  "last_result": {
    "success": true,
    "message": "Scheduler completed for 2 users",
    "results": [
      {
        "user_id": "user1",
        "result": {
          "success": true,
          "message": "Successfully processed user user1"
        }
      }
    ]
  }
}
```

## React Integration

### Option 1: Full Featured Component

Use the `SchedulerTrigger.jsx` component for a complete UI with status tracking:

```jsx
import SchedulerTrigger from './SchedulerTrigger';

function App() {
  return (
    <div>
      <h1>My Email Dashboard</h1>
      <SchedulerTrigger />
    </div>
  );
}
```

### Option 2: Simple Button

Use the `SimpleSchedulerButton.jsx` for basic functionality:

```jsx
import SimpleSchedulerButton from './SimpleSchedulerButton';

function MyComponent() {
  const handleSuccess = (result) => {
    console.log('Scheduler completed:', result);
    // Show success notification
  };

  const handleError = (error) => {
    console.error('Scheduler failed:', error);
    // Show error notification
  };

  return (
    <SimpleSchedulerButton
      apiBaseUrl="https://your-flask-app.onrender.com"
      onSuccess={handleSuccess}
      onError={handleError}
    />
  );
}
```

### Option 3: Custom Implementation

```jsx
import React, { useState } from 'react';

function CustomSchedulerButton() {
  const [loading, setLoading] = useState(false);

  const runScheduler = async () => {
    setLoading(true);
    
    try {
      const response = await fetch('https://your-flask-app.onrender.com/api/trigger-scheduler', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          // 'X-API-Key': 'your-api-key' // if you add authentication
        }
      });

      const result = await response.json();
      
      if (response.ok) {
        alert('Scheduler started successfully!');
      } else {
        alert(`Error: ${result.error}`);
      }
    } catch (error) {
      alert('Failed to start scheduler');
    } finally {
      setLoading(false);
    }
  };

  return (
    <button onClick={runScheduler} disabled={loading}>
      {loading ? 'Starting...' : 'Run Email Scheduler'}
    </button>
  );
}
```

## Security Considerations

### Adding API Key Authentication

To add security, modify the Flask endpoint to require an API key:

```python
@app.route("/api/trigger-scheduler", methods=["POST"])
def api_trigger_scheduler():
    # Check for API key
    api_key = request.headers.get('X-API-Key')
    expected_key = os.getenv('SCHEDULER_API_KEY')
    
    if not api_key or api_key != expected_key:
        return jsonify({"error": "Unauthorized"}), 401
    
    # ... rest of the function
```

Then set the environment variable:
```bash
export SCHEDULER_API_KEY="your-secret-api-key"
```

### CORS Configuration

If your React app is on a different domain, add CORS support:

```python
from flask_cors import CORS

app = Flask(__name__)
CORS(app, origins=["https://your-react-app.com"])
```

## Deployment Notes

1. **Environment Variables**: Make sure all required environment variables are set in your Flask app deployment
2. **URL Configuration**: Update the `API_BASE_URL` in your React components to match your deployed Flask app
3. **Rate Limiting**: Consider adding rate limiting to prevent abuse
4. **Monitoring**: The scheduler runs in a background thread, so monitor your server resources

## Testing

Test the API endpoints using curl:

```bash
# Trigger scheduler
curl -X POST https://your-flask-app.onrender.com/api/trigger-scheduler \
  -H "Content-Type: application/json"

# Check status
curl https://your-flask-app.onrender.com/api/scheduler-status
```

## What the Scheduler Does

The scheduler performs the same operations as your GitHub Actions workflow:

1. **Processes all users** with stored authentication tokens
2. **Sends outbound emails** from the outbox collection in Firestore
3. **Scans inboxes** for replies and matches them to existing conversations
4. **Updates Firestore** with processed email data
5. **Handles authentication** token refresh automatically

This allows you to run email processing on-demand instead of waiting for the next scheduled run.
