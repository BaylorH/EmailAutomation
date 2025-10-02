import React, { useState } from 'react';

const SimpleSchedulerButton = ({ 
  apiBaseUrl = 'https://email-token-manager.onrender.com',
  apiKey = null,
  onSuccess = null,
  onError = null 
}) => {
  const [isRunning, setIsRunning] = useState(false);
  const [loading, setLoading] = useState(false);

  const triggerScheduler = async () => {
    setLoading(true);
    
    try {
      const response = await fetch(`${apiBaseUrl}/api/trigger-scheduler`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(apiKey && { 'X-API-Key': apiKey })
        }
      });

      const result = await response.json();
      
      if (response.ok) {
        setIsRunning(true);
        if (onSuccess) onSuccess(result);
        
        // Poll for completion
        const pollInterval = setInterval(async () => {
          try {
            const statusResponse = await fetch(`${apiBaseUrl}/api/scheduler-status`);
            const status = await statusResponse.json();
            
            if (!status.running) {
              setIsRunning(false);
              clearInterval(pollInterval);
              if (onSuccess) onSuccess(status.last_result);
            }
          } catch (error) {
            console.error('Error polling status:', error);
            clearInterval(pollInterval);
            setIsRunning(false);
          }
        }, 3000);
        
      } else {
        if (onError) onError(result.error || 'Failed to trigger scheduler');
      }
    } catch (error) {
      console.error('Error triggering scheduler:', error);
      if (onError) onError('Network error occurred');
    } finally {
      setLoading(false);
    }
  };

  return (
    <button
      onClick={triggerScheduler}
      disabled={isRunning || loading}
      className="scheduler-trigger-btn"
    >
      {loading ? 'Starting...' : isRunning ? 'Running...' : 'Run Email Scheduler'}
    </button>
  );
};

export default SimpleSchedulerButton;
