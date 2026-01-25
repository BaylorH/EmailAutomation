# Email Automation System Architecture

## High-Level System Overview

```mermaid
flowchart TB
    subgraph Frontend["Frontend (email-admin-ui)"]
        UI[React Dashboard]
        Upload[Excel Upload]
        Clients[Client Management]
        Chat[AI Chat Interface]
        Notifs[Notifications Sidebar]
    end

    subgraph Firebase["Firebase"]
        Auth[Firebase Auth]
        FS[(Firestore)]
        Storage[Firebase Storage]
        Functions[Cloud Functions]
    end

    subgraph Backend["Backend (EmailAutomation)"]
        Scheduler[Scheduler / main.py]
        Processing[processing.py]
        AI[ai_processing.py]
        Email[email.py]
        Sheets[sheets.py]
    end

    subgraph External["External Services"]
        Graph[Microsoft Graph API]
        OpenAI[OpenAI GPT-4o]
        GSheets[Google Sheets API]
        GDrive[Google Drive]
    end

    UI --> Auth
    Upload --> Functions
    Functions --> GSheets
    Clients --> FS
    Chat --> Functions

    FS <--> Backend
    Storage --> Backend

    Scheduler --> Processing
    Processing --> AI
    Processing --> Email
    AI --> OpenAI
    Email --> Graph
    Processing --> Sheets
    Sheets --> GSheets

    Graph --> Processing

    Backend --> FS
    FS --> Notifs
```

## Data Flow - Campaign Lifecycle

```mermaid
sequenceDiagram
    participant User as Jill (User)
    participant FE as Frontend
    participant FS as Firestore
    participant BE as Backend
    participant AI as OpenAI
    participant MS as Microsoft Graph
    participant GS as Google Sheets

    Note over User,GS: Campaign Setup
    User->>FE: Upload Scrub Excel
    FE->>FS: Create client + properties
    FE->>GS: Create Google Sheet
    User->>FE: Launch campaign
    FE->>FS: Write to outbox/

    Note over User,GS: Email Sending (every 30 min)
    BE->>FS: Read outbox/
    BE->>MS: Send emails via Graph API
    BE->>FS: Index in msgIndex/, convIndex/
    BE->>FS: Delete from outbox/

    Note over User,GS: Reply Processing
    MS-->>BE: Broker replies to inbox
    BE->>FS: Match via msgIndex/convIndex
    BE->>AI: Extract property data
    AI-->>BE: {updates, events, notes, response}

    alt All Fields Complete
        BE->>GS: Write to sheet
        BE->>FS: Notify: row_completed
        BE->>MS: Send thank-you email
    else Missing Fields
        BE->>GS: Write partial data
        BE->>FS: Notify: sheet_update
        BE->>MS: Send follow-up request
    else Escalation Needed
        BE->>FS: Notify: action_needed
        Note over BE: No auto-response
    end

    FS-->>FE: Real-time notification
    FE-->>User: Show in sidebar
```

## Firestore Data Structure

```mermaid
erDiagram
    USERS ||--o{ CLIENTS : has
    USERS ||--o{ OUTBOX : queues
    USERS ||--o{ THREADS : stores
    USERS ||--o{ MSG_INDEX : indexes
    USERS ||--o{ CONV_INDEX : indexes
    USERS ||--o{ OPTED_OUT : tracks

    CLIENTS ||--o{ NOTIFICATIONS : receives
    THREADS ||--o{ MESSAGES : contains

    USERS {
        string uid PK
        string displayName
        string emailSignature
        string profilePic
        string organizationName
    }

    CLIENTS {
        string clientId PK
        string name
        string sheetId
        array assignedEmails
        object criteria
    }

    NOTIFICATIONS {
        string kind
        string priority
        object meta
        timestamp createdAt
    }

    OUTBOX {
        string clientId
        string script
        string subject
        object property
        boolean isPersonalized
    }

    THREADS {
        string threadId PK
        string clientId
        string email
        string rowAnchor
        int rowIndex
    }

    MESSAGES {
        string direction
        string body
        string from
        timestamp timestamp
    }

    MSG_INDEX {
        string messageId PK
        string threadId
    }

    CONV_INDEX {
        string conversationId PK
        string threadId
    }
```

## AI Processing Pipeline

```mermaid
flowchart LR
    subgraph Input
        Conv[Conversation History]
        Props[Property Data]
        Sheet[Sheet Row State]
    end

    subgraph Prompt["Prompt Assembly"]
        Rules[Extraction Rules]
        Notes[Notes Rules]
        Events[Event Detection]
        Response[Response Rules]
    end

    subgraph AI["OpenAI GPT-4o"]
        Extract[Field Extraction]
        Detect[Event Detection]
        Generate[Response Generation]
        Context[Notes Capture]
    end

    subgraph Output
        Updates[Sheet Updates]
        EventsOut[Events]
        Email[Response Email]
        NotesOut[Notes]
    end

    Conv --> Prompt
    Props --> Prompt
    Sheet --> Prompt

    Rules --> AI
    Notes --> AI
    Events --> AI
    Response --> AI

    AI --> Updates
    AI --> EventsOut
    AI --> Email
    AI --> NotesOut
```

## Event Types & Handling

```mermaid
flowchart TD
    Reply[Broker Reply] --> AI{AI Analysis}

    AI -->|Complete Info| Complete[row_completed]
    AI -->|Partial Info| Partial[sheet_update + follow-up]
    AI -->|Unavailable| Unavail[property_unavailable]
    AI -->|New Property| NewProp[new_property]
    AI -->|Tour Offer| Tour[tour_requested]
    AI -->|Call Request| Call[call_requested]
    AI -->|Identity Question| Identity[needs_user_input:confidential]
    AI -->|Budget Question| Budget[needs_user_input:client_question]
    AI -->|Negotiation| Negotiate[needs_user_input:negotiation]
    AI -->|Contract Request| Contract[needs_user_input:legal_contract]
    AI -->|Opt Out| OptOut[contact_optout]
    AI -->|Wrong Person| Wrong[wrong_contact]
    AI -->|Property Issue| Issue[property_issue]

    Complete --> AutoReply[Auto-send thank you]
    Partial --> AutoReply2[Auto-send follow-up]
    Unavail --> MoveRow[Move below NON-VIABLE]
    NewProp --> UserApproval[User approves new property]
    Tour --> UserDecision[User decides on tour]
    Call --> UserAction[User calls broker]
    Identity --> UserInput[User provides answer]
    Budget --> UserInput
    Negotiate --> UserInput
    Contract --> UserInput
    OptOut --> AddToList[Add to opted-out list]
    Wrong --> UserAction2[User redirects]
    Issue --> UserReview[User reviews issue]
```

## Testing Infrastructure

```mermaid
flowchart TB
    subgraph TestTypes["Test Types"]
        Standalone[standalone_test.py<br/>25 scenarios]
        E2E[e2e_test.py<br/>11 conversations]
        Campaign[campaign_lifecycle_test.py<br/>11 scenarios]
        Quality[quality_benchmark.py<br/>8 benchmarks]
        Batch[batch_runner.py<br/>559+ tests]
    end

    subgraph TestData["Test Data"]
        Scrub[Scrub Augusta GA.xlsx]
        Convos[conversations/*.json]
        Generated[generated_suite/]
    end

    subgraph Validation["Validation"]
        FieldAcc[Field Accuracy]
        Events[Event Detection]
        Forbidden[Forbidden Fields]
        Notes[Notes Quality]
        Response[Response Quality]
    end

    subgraph Results["Results"]
        Pass[Pass/Fail]
        Metrics[Latency Metrics]
        Reports[HTML Reports]
        JSON[JSON Results]
    end

    Scrub --> E2E
    Scrub --> Campaign
    Convos --> E2E
    Generated --> Batch

    Standalone --> Validation
    E2E --> Validation
    Campaign --> Validation
    Quality --> Validation
    Batch --> Validation

    Validation --> Results
```

## Sheet Column Flow

```mermaid
flowchart LR
    subgraph Columns["Sheet Columns"]
        ReadOnly[READ-ONLY<br/>Property Address<br/>City<br/>Leasing Contact<br/>Email]
        Extractable[EXTRACTABLE<br/>Total SF<br/>Rent/SF /Yr<br/>Ops Ex /SF<br/>Drive Ins<br/>Docks<br/>Ceiling Ht<br/>Power]
        Formula[FORMULA<br/>Gross Rent]
        Comments[COMMENTS<br/>Listing Brokers Comments<br/>Jill and Clients comments]
        Links[LINKS<br/>Flyer / Link<br/>Floorplan]
    end

    subgraph Rules["AI Rules"]
        Never[NEVER Update]
        Extract[Extract from emails]
        NeverWrite[NEVER Write]
        Append[Append notes]
        Capture[Capture URLs]
    end

    ReadOnly --> Never
    Extractable --> Extract
    Formula --> NeverWrite
    Comments --> Append
    Links --> Capture
```

## Notes Quality Criteria

```mermaid
mindmap
  root((Notes Column))
    Should Include
      Lease Type
        NNN
        Gross
        Modified Gross
      Availability
        Immediate
        Date specific
        Notice period
      Landlord Info
        Motivated
        Firm on price
        Flexible terms
      Features
        Fenced yard
        Rail spur
        Sprinklered
        ESFR
      Location
        Near highways
        Airport adjacent
        Industrial park
      Deal Terms
        TI allowance
        Sublease info
        Zoning
    Should NOT Include
      Column Values
        Rent amounts
        Square footage
        Ops Ex
        Door counts
        Ceiling height
        Power specs
```

## Production State

```mermaid
pie title Current Production Status
    "User Profiles" : 2
    "Operational Data" : 0
    "MSAL Tokens" : 2
```

## Quality Metrics

```mermaid
xychart-beta
    title "AI Quality Scores"
    x-axis ["Field Accuracy", "Completeness", "Notes Quality", "Response Quality", "Event Accuracy", "Overall"]
    y-axis "Score %" 0 --> 100
    bar [95.8, 95.8, 74.9, 76.8, 87.5, 87.0]
```
