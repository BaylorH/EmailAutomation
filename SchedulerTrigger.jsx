import React, { useState, useEffect } from 'react';

const SchedulerTrigger = () => {
  const [isRunning, setIsRunning] = useState(false);
  const [lastResult, setLastResult] = useState(null);
  const [lastRun, setLastRun] = useState(null);
  const [loading, setLoading] = useState(false);

  // Replace this with your actual Flask app URL
  const API_BASE_URL = 'https://email-token-manager.onrender.com';
  
  // Optional: Add your API key here for authentication
  const API_KEY = 'your-api-key-here'; // You can set this in your environment

  // Check scheduler status on component mount and periodically
  useEffect(() => {
    checkSchedulerStatus();
    const interval = setInterval(checkSchedulerStatus, 5000); // Check every 5 seconds
    return () => clearInterval(interval);
  }, []);

  const checkSchedulerStatus = async () => {
    try {
      const response = await fetch(`${API_BASE_URL}/api/scheduler-status`, {
        method: 'GET',
        headers: {
          'Content-Type': 'application/json',
          ...(API_KEY && { 'X-API-Key': API_KEY })
        }
      });
      
      if (response.ok) {
        const status = await response.json();
        setIsRunning(status.running);
        setLastResult(status.last_result);
        setLastRun(status.last_run);
      }
    } catch (error) {
      console.error('Error checking scheduler status:', error);
    }
  };

  const triggerScheduler = async () => {
    setLoading(true);
    try {
      const response = await fetch(`${API_BASE_URL}/api/trigger-scheduler`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(API_KEY && { 'X-API-Key': API_KEY })
        }
      });

      const result = await response.json();
      
      if (response.ok) {
        setIsRunning(true);
        // Start polling for status updates more frequently
        const pollInterval = setInterval(async () => {
          await checkSchedulerStatus();
          // Stop polling when scheduler is no longer running
          if (!isRunning) {
            clearInterval(pollInterval);
          }
        }, 2000);
      } else {
        alert(`Error: ${result.error || 'Failed to trigger scheduler'}`);
      }
    } catch (error) {
      console.error('Error triggering scheduler:', error);
      alert('Failed to trigger scheduler. Please try again.');
    } finally {
      setLoading(false);
    }
  };

  const formatDateTime = (isoString) => {
    if (!isoString) return 'Never';
    return new Date(isoString).toLocaleString();
  };

  const getStatusColor = () => {
    if (isRunning) return '#f59e0b'; // yellow/orange for running
    if (lastResult?.success) return '#10b981'; // green for success
    if (lastResult?.success === false) return '#ef4444'; // red for error
    return '#6b7280'; // gray for unknown
  };

  const getStatusText = () => {
    if (isRunning) return 'Running...';
    if (lastResult?.success) return 'Last run: Success';
    if (lastResult?.success === false) return 'Last run: Failed';
    return 'Ready';
  };

  return (
    <div style={{
      padding: '20px',
      border: '1px solid #e5e7eb',
      borderRadius: '8px',
      backgroundColor: '#ffffff',
      boxShadow: '0 1px 3px rgba(0, 0, 0, 0.1)',
      maxWidth: '400px',
      margin: '20px auto'
    }}>
      <h3 style={{ 
        margin: '0 0 16px 0', 
        color: '#1f2937',
        fontSize: '18px',
        fontWeight: '600'
      }}>
        📧 Email Scheduler
      </h3>
      
      {/* Status indicator */}
      <div style={{
        display: 'flex',
        alignItems: 'center',
        marginBottom: '16px',
        padding: '12px',
        backgroundColor: '#f9fafb',
        borderRadius: '6px',
        border: `2px solid ${getStatusColor()}`
      }}>
        <div style={{
          width: '12px',
          height: '12px',
          borderRadius: '50%',
          backgroundColor: getStatusColor(),
          marginRight: '8px',
          ...(isRunning && {
            animation: 'pulse 2s infinite'
          })
        }}></div>
        <span style={{ 
          fontWeight: '500',
          color: '#374151'
        }}>
          {getStatusText()}
        </span>
      </div>

      {/* Trigger button */}
      <button
        onClick={triggerScheduler}
        disabled={isRunning || loading}
        style={{
          width: '100%',
          padding: '12px 16px',
          backgroundColor: isRunning || loading ? '#9ca3af' : '#3b82f6',
          color: 'white',
          border: 'none',
          borderRadius: '6px',
          fontSize: '16px',
          fontWeight: '500',
          cursor: isRunning || loading ? 'not-allowed' : 'pointer',
          transition: 'background-color 0.2s',
          marginBottom: '16px'
        }}
        onMouseOver={(e) => {
          if (!isRunning && !loading) {
            e.target.style.backgroundColor = '#2563eb';
          }
        }}
        onMouseOut={(e) => {
          if (!isRunning && !loading) {
            e.target.style.backgroundColor = '#3b82f6';
          }
        }}
      >
        {loading ? '⏳ Starting...' : isRunning ? '🔄 Running...' : '🚀 Run Email Scheduler'}
      </button>

      {/* Last run info */}
      {lastRun && (
        <div style={{
          fontSize: '14px',
          color: '#6b7280',
          marginBottom: '8px'
        }}>
          <strong>Last run:</strong> {formatDateTime(lastRun)}
        </div>
      )}

      {/* Results */}
      {lastResult && (
        <div style={{
          fontSize: '14px',
          padding: '8px',
          borderRadius: '4px',
          backgroundColor: lastResult.success ? '#f0fdf4' : '#fef2f2',
          border: `1px solid ${lastResult.success ? '#bbf7d0' : '#fecaca'}`,
          color: lastResult.success ? '#166534' : '#dc2626'
        }}>
          <strong>Result:</strong> {lastResult.message || lastResult.error}
          {lastResult.results && (
            <div style={{ marginTop: '4px', fontSize: '12px' }}>
              Processed {lastResult.results.length} user(s)
            </div>
          )}
        </div>
      )}

      {/* Info text */}
      <div style={{
        fontSize: '12px',
        color: '#9ca3af',
        marginTop: '12px',
        textAlign: 'center'
      }}>
        This runs the same email processing that GitHub Actions runs every 30 minutes
      </div>

      {/* CSS for pulse animation */}
      <style jsx>{`
        @keyframes pulse {
          0%, 100% {
            opacity: 1;
          }
          50% {
            opacity: 0.5;
          }
        }
      `}</style>
    </div>
  );
};

export default SchedulerTrigger;
