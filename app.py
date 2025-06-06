import streamlit as st
import google.generativeai as genai
import time
from datetime import datetime, date
import psycopg2 # For PostgreSQL
import psycopg2.extras # For dictionary-like cursors
import json
from werkzeug.security import generate_password_hash, check_password_hash
import pandas as pd
import io
from streamlit_quill import st_quill
from markdownify import markdownify as md
import uuid # For generating IDs if needed, though SERIAL PRIMARY KEY handles it

# --- Page Configuration ---
st.set_page_config(
    layout="wide",
    page_title="Truskill AI Essay Grader",
    page_icon="https://truskill.in/images/logo/logo.png"
)
APP_LOGO_URL = "https://truskill.in/images/logo/logo.png"

# --- Gemini API Key ---
# We rely on st.secrets for deployment
try:
    GEMINI_API_KEY = st.secrets["GEMINI_API_KEY"]
except KeyError:
    st.error("🚨 Gemini API Key not found in Streamlit Secrets.")
    st.stop() # Critical failure, cannot proceed without API key

genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel(model_name='gemini-1.5-flash-latest')

# --- Database Connection Function (PostgreSQL) ---
# Keep the same connection logic, but potentially simplify error display for users
def get_db_connection():
    try:
        db_url = st.secrets["DATABASE_URL"]
        conn = psycopg2.connect(db_url)
        return conn
    except Exception as e:
        # Log the detailed error, but show a simpler message to the user
        print(f"DB Connection Error: {e}")
        st.error("🚨 Could not connect to the database. Please contact support if this persists.")
        return None

# --- Database Initialization Function (PostgreSQL) ---
def initialize_database_schema():
    print(f"[{datetime.now()}] Attempting to initialize PostgreSQL schema...") # For logs
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        if conn is None:
            # If connection fails, get_db_connection already shows error, just print log
            print("DB connection failed in schema initialization.")
            return

        cursor = conn.cursor()
        # Schema creation remains the same - it's robust
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                user_type TEXT NOT NULL CHECK(user_type IN ('student', 'college_admin', 'super_admin')),
                college_name TEXT
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS student_profiles (
                user_id INTEGER PRIMARY KEY, -- This links to users.id
                full_name TEXT NOT NULL,
                department TEXT NOT NULL,
                branch TEXT, -- Nullable
                roll_number TEXT, -- Nullable
                email TEXT, -- Nullable
                FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS essays (
                id SERIAL PRIMARY KEY,
                student_user_id INTEGER NOT NULL, -- This links to users.id
                title TEXT NOT NULL,
                content_markdown TEXT NOT NULL,
                submission_time TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                ai_feedback_json JSONB,
                overall_rating REAL, -- Nullable
                FOREIGN KEY (student_user_id) REFERENCES users (id) ON DELETE CASCADE
            )
        ''')
        conn.commit()
        print(f"[{datetime.now()}] PostgreSQL schema creation/check committed.")

        # Add default super_admin if not exists
        cursor.execute("SELECT id FROM users WHERE username = %s", ('mainadmin',))
        if cursor.fetchone() is None:
            cursor.execute("INSERT INTO users (username, password_hash, user_type, college_name) VALUES (%s, %s, %s, %s)",
                           ('mainadmin', generate_password_hash('superpassword123'), 'super_admin', None))
            conn.commit()
            print(f"[{datetime.now()}] Default super_admin added to PostgreSQL.")

    except (Exception, psycopg2.Error) as error:
        # Log detailed error for debugging, but show simpler message to user (or rely on conn error)
        print(f"PostgreSQL initialization error: {error}")
        # st.error("🚨 Initial database setup failed.") # Can uncomment if needed
    finally:
        if cursor: cursor.close()
        if conn: conn.close()
        print(f"[{datetime.now()}] PostgreSQL initialization routine finished.")

# --- Execute schema initialization (conditionally, once per app session/process) ---
# This check is important for efficiency, but the init function itself handles idempotency
if 'db_schema_initialized' not in st.session_state:
    initialize_database_schema()
    st.session_state.db_schema_initialized = True

# --- Authentication and User Data Functions (DEFINED BEFORE AI & UI) ---

def create_user(username, password, user_type, college_name=None):
    sql = "INSERT INTO users (username, password_hash, user_type, college_name) VALUES (%s, %s, %s, %s)"
    conn = None
    cursor = None
    try:
        conn = get_db_connection();
        if conn is None: return False, "Database error during user creation." # Simplified error
        cursor = conn.cursor()
        cursor.execute(sql, (username, generate_password_hash(password), user_type, college_name))
        conn.commit()
        print(f"[{datetime.now()}] User created successfully: {username}")
        return True, "Account created successfully. Please log in." # Simplified success message
    except (Exception, psycopg2.Error) as error:
        print(f"Error creating user {username}: {error}") # Log detailed error
        if isinstance(error, psycopg2.IntegrityError) and "users_username_key" in str(error).lower():
             return False, "Username already exists."
        return False, f"An error occurred during account creation." # Simplified generic error
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def authenticate_user(username, password):
    conn = None
    cursor = None
    # print(f"[{datetime.now()}] Attempting authentication for user: {username}") # Debug print
    try:
        conn = get_db_connection();
        if conn is None:
            # print(f"[{datetime.now()}] Auth failed: get_db_connection returned None.") # Debug print
            # get_db_connection already shows error, no need to repeat
            return
        # print(f"[{datetime.now()}] Auth: Database connection successful.") # Debug print

        cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        # print(f"[{datetime.now()}] Auth: Cursor created. Executing query for user {username}...") # Debug print
        # *** FIX APPLIED HERE: Added 'username' to the SELECT list ***
        cursor.execute("SELECT id, username, password_hash, user_type, college_name FROM users WHERE username = %s", (username,))
        user_record = cursor.fetchone() # Returns a DictRow or None
        # print(f"[{datetime.now()}] Auth: Query executed. user_record: {user_record}") # Debug print

        if user_record:
            # print(f"[{datetime.now()}] Auth: User record found. Checking password hash...") # Debug print
            if check_password_hash(user_record['password_hash'], password):
                # print(f"[{datetime.now()}] Auth: Password hash matched! Setting session state...") # Debug print
                st.session_state.logged_in = True
                st.session_state.user_type = user_record['user_type']
                # Access username from the fetched record using the column name
                st.session_state.current_username = user_record.get('username')
                st.session_state.current_user_id = user_record['id']
                st.session_state.current_college_name = user_record.get('college_name')

                # After successful login, determine the next view based on user type
                # print(f"[{datetime.now()}] Auth: User type is {st.session_state.user_type}. Determining next view...") # Debug print
                if st.session_state.user_type == 'student':
                     # Students go directly to the essay writing page after login
                     st.session_state.view = 'student_essay'
                     # Reset essay state on successful login & redirect to essay
                     st.session_state.essay_started = False
                     st.session_state.timer_start_time = None
                     st.session_state.essay_title_input = ""
                     st.session_state.essay_content_html = ""
                     st.session_state.pop('profile_page_loaded', None) # Clear profile flag on login
                     # print(f"[{datetime.now()}] Auth: Redirecting student to essay view.") # Debug print
                else:
                    st.session_state.view = 'dashboard' # Admins go to general dashboard view
                    # print(f"[{datetime.now()}] Auth: Redirecting admin to dashboard view.") # Debug print

                st.success(f"Logged in successfully!") # Simplified success message
                # The actual welcome message with username is in the sidebar UI logic
                print(f"[{datetime.now()}] User {username} logged in successfully. Session User ID: {st.session_state.current_user_id}") # Debug print
                st.rerun() # This triggers a script rerun

            else:
                # print(f"[{datetime.now()}] Auth failed: Password hash did NOT match for user {username}.") # Debug print
                st.error("Invalid username or password") # Keep this user feedback
        else:
             # print(f"[{datetime.now()}] Auth failed: User record NOT found for username {username}.") # Debug print
             st.error("Invalid username or password") # Keep this user feedback

    except (Exception, psycopg2.Error) as error:
        print(f"[{datetime.now()}] Auth Error for user {username}: {error}") # Log detailed error
        st.error(f"An authentication error occurred. Please try again.") # Simplified user error
    finally:
        if cursor: cursor.close()
        if conn: conn.close()
        # print(f"[{datetime.now()}] Authentication routine finished for user {username}.") # Debug print


def get_student_profile(user_id):
    if user_id is None:
        print(f"[{datetime.now()}] get_student_profile called with user_id = None. This should ideally not happen after login.") # Debug print
        return None # Return None if user_id is unexpectedly missing
    conn = None
    cursor = None
    try:
        conn = get_db_connection();
        if conn is None: return None
        cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cursor.execute("SELECT full_name, department, branch, roll_number, email FROM student_profiles WHERE user_id = %s", (user_id,))
        profile = cursor.fetchone() # Returns a DictRow or None
        # print(f"[{datetime.now()}] Fetched student profile for user {user_id}: {profile}") # Debug print
        return profile
    except (Exception, psycopg2.Error) as error:
        print(f"Error getting student profile for user {user_id}: {error}") # Log detailed error
        # Do not show error to user here, just return None
        return None
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def save_student_profile(user_id, full_name, department, branch, roll_number, email):
    if user_id is None:
        print(f"[{datetime.now()}] save_student_profile called with user_id = None. This is unexpected.") # Debug print
        st.error("Could not save profile: User session issue. Please try logging out and in again.") # Simplified user error
        return False
    sql = """
        INSERT INTO student_profiles (user_id, full_name, department, branch, roll_number, email)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (user_id) DO UPDATE SET
            full_name = EXCLUDED.full_name,
            department = EXCLUDED.department,
            branch = EXCLUDED.branch,
            roll_number = EXCLUDED.roll_number,
            email = EXCLUDED.email;
    """
    conn = None
    cursor = None
    try:
        conn = get_db_connection();
        if conn is None:
            st.error("Failed to save profile: Database connection error.") # Keep this for user feedback
            return False
        cursor = conn.cursor()
        cursor.execute(sql, (user_id, full_name, department, branch, roll_number, email))
        conn.commit()
        print(f"[{datetime.now()}] Student profile saved/updated for user_id: {user_id}") # Debug print
        return True
    except (Exception, psycopg2.Error) as error:
        print(f"[{datetime.now()}] Error saving student profile for user_id {user_id}: {error}") # Log detailed error
        st.error("Failed to save profile due to an internal error.") # Simplified user error
        return False
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def save_essay_submission(student_user_id, title, content_markdown, ai_feedback_json_str, overall_rating):
    """
    Inserts a new essay record into the essays table.
    """
    if student_user_id is None:
         print(f"[{datetime.now()}] save_essay_submission called with student_user_id = None.")
         st.error("Cannot save essay: User ID is not available. Please log out and log in again.")
         return False # Indicate failure
    sql = """
        INSERT INTO essays (student_user_id, title, content_markdown, submission_time, ai_feedback_json, overall_rating)
        VALUES (%s, %s, %s, %s, %s, %s)
    """
    conn = None
    cursor = None
    submission_time_val = datetime.now()
    try:
        conn = get_db_connection();
        if conn is None:
            st.error("Failed to save essay: Database connection error.")
            return False # Indicate failure
        cursor = conn.cursor()
        db_overall_rating = overall_rating if isinstance(overall_rating, (int, float)) else None
        cursor.execute(sql, (student_user_id, title, content_markdown, submission_time_val, ai_feedback_json_str, db_overall_rating))
        conn.commit()
        print(f"[{datetime.now()}] Essay saved successfully for user {student_user_id}")
        return True # Indicate success
    except (Exception, psycopg2.Error) as error:
        st.error("Failed to save essay submission.")
        print(f"Error saving essay for user {student_user_id}: {error}")
        return False # Indicate failure
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def get_student_essays(student_user_id):
    if student_user_id is None: return [] # Return empty list if user_id is missing
    conn = None
    cursor = None
    try:
        conn = get_db_connection();
        if conn is None: return []
        cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cursor.execute('''
            SELECT id, title, content_markdown, submission_time, ai_feedback_json, overall_rating
            FROM essays
            WHERE student_user_id = %s
            ORDER BY submission_time DESC
        ''', (student_user_id,))
        essays = [dict(row) for row in cursor.fetchall()]
        # print(f"[{datetime.now()}] Fetched {len(essays)} essays for user {student_user_id}.") # Debug print
        return essays
    except (Exception, psycopg2.Error) as error:
        print(f"Error getting student essays for user {student_user_id}: {error}") # Log detailed error
        return [] # Return empty list on error
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def get_college_reports(college_name):
    # This function is primarily for admin roles, less focus on simplifying user-facing messages here
    conn = None
    cursor = None
    sql_query = '''
        SELECT
            e.id as essay_id, e.title as essay_title, e.submission_time, e.overall_rating, e.ai_feedback_json, e.content_markdown,
            u.username as student_username, u.college_name,
            sp.full_name as student_full_name,
            sp.department as student_department,
            sp.branch as student_branch,
            sp.roll_number as student_roll_number
        FROM essays e
        JOIN users u ON e.student_user_id = u.id
        LEFT JOIN student_profiles sp ON u.id = sp.user_id
        WHERE u.college_name = %s AND u.user_type = 'student'
    '''
    try:
        conn = get_db_connection();
        if conn is None:
             st.error("Failed to fetch college reports: Database error.") # Keep this for admin
             return []
        cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cursor.execute(sql_query, (college_name,))
        reports_list = [dict(row) for row in cursor.fetchall()]
        # print(f"[{datetime.now()}] Fetched {len(reports_list)} college reports for {college_name}.") # Debug print
        return reports_list
    except (Exception, psycopg2.Error) as error:
        print(f"SQL Error in get_college_reports for {college_name}: {error}") # Log detailed error
        st.error("Failed to fetch college reports due to an internal error.") # Simplified admin error
        return []
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

# --- Helper Functions ---
def logout():
    # Reset all relevant session state variables on logout
    keys_to_reset = ['logged_in', 'user_type', 'current_username', 'current_user_id',
                     'current_college_name', 'essay_started', 'timer_start_time',
                     'essay_title_input', 'essay_content_html', 'view',
                     'profile_page_loaded'] # Add profile_page_loaded to reset list
    for key in keys_to_reset:
        if key in st.session_state:
            del st.session_state[key]
    st.session_state.logged_in = False # Ensure this is explicitly False
    st.session_state.view = 'login' # Always return to login view
    st.success("Logged out.")
    st.rerun()

def calculate_word_count(text):
    return len(text.split()) if text else 0

# --- AI Logic Functions (DEFINED BEFORE UI SECTIONS, BELOW AUTH FUNCTIONS) ---
def get_gemini_assessment(title, essay_markdown):
    prompt = f"""
    You are an Excellent Writter and Copywrite Expert which has 20 years of experience in writing essays. You are specialized in evaluating student essays. You given a Title and Essay in Markdown format below, Asses Students Essay in Right manner.
    The essay title is: "{title}"
    The essay content (in Markdown) is:
    ---
    {essay_markdown}
    ---
    Please assess the essay based on the following five criteria. For each, give a score from 0 to 100 (0 = very poor, 100 = excellent) and a brief justification:

    1. Grammar (20%): spelling, punctuation, sentence structure, mechanics of Writting. 
    2. Relevancy and Cohesion with Title (25%): how well the content stays on topic mentioned  and flows logically relative to the title.  
    3. Clarity and Content Development with respect to Title (25%): depth of ideas present in content, supporting evidence, originality, and clarity with relate to the title.  
    4. Sentence Formation (20%): variety and complexity of sentence structures, conciseness. 
    5. Formatting (10%): appropriate Markdown usage (headings, lists, blockquotes) and overall readability and Presentation.

    After scoring, also provide:
    - Overall Word Count  
    - Overall Feedback: a concise summary (4 to 6 sentences) of the essays main strengths and areas need to improve. 
    - Overall Rating: a single number from 0 to 100, computed by applying the above weights  

    Output **only** the following JSON object (no extra text), with all strings properly escaped:

    {{
      "criteria_scores": {{
        "grammar": {{"score": <int_0_to_100>, "justification": "<string>"}},
        "relevancy_and_cohesion": {{"score": <int_0_to_100>, "justification": "<string>"}},
        "clarity_and_content_development_with_respect_to_title": {{"score": <int_0_to_100>, "justification": "<string>"}},
        "sentence_formation": {{"score": <int_0_to_100>, "justification": "<string>"}},
        "formatting": {{"score": <int_0_to_100>, "justification": "<string>"}}
      }},
      "word_count": <int_word_count>,
      "overall_feedback": "<string>",
      "overall_rating": <int_0_to_100>
    }}
    """
    response = gemini_model.generate_content(prompt)
    response_text = response.text if hasattr(response, 'text') else ''
    # JSON extraction logic unchanged...

    response = gemini_model.generate_content(prompt)
    # …rest of your parsing logic…

    response = None # Initialize response to None
    response_text = None # Initialize response_text to None

    try:
        response = gemini_model.generate_content(prompt) # This is the line that might raise an error before assigning to response
        response_text = response.text # This line might also fail if response doesn't have .text

        # Keep the JSON parsing logic the same - it seems robust
        if response_text is not None: # Add a check here before trying to parse
            if response_text.strip().startswith("```json"):
                response_text = response_text.strip()[7:-3].strip()
            elif response_text.strip().startswith("```"):
                 response_text = response_text.strip()[3:-3].strip()
            json_start_index = response_text.find('{')
            json_end_index = response_text.rfind('}') + 1
            if json_start_index != -1 and json_end_index != -1 :
                json_string = response_text[json_start_index:json_end_index]
                parsed_response = json.loads(json_string)
                return parsed_response
            else:
                print(f"AI Response JSON parse failure. Raw: {response_text}") # Log raw response
                return {"error": "AI feedback format issue.", "raw_response": response_text} # Simplified error
        else:
             print("AI Response text was None.") # Log this unexpected state
             return {"error": "AI response text is empty or missing."}

    except json.JSONDecodeError as e:
        # This block is reached if JSON decoding fails *after* response_text is assigned
        print(f"Error decoding JSON from AI: {e}. Raw response: {response_text}") # response_text should be available here
        raw_resp_info = response_text if response_text is not None else "Response text was None or unassignable"
        return {"error": f"AI feedback parsing error: {e}", "raw_response": raw_resp_info} # Simplified error

    except Exception as e:
        # *** This block is reached for any other exception during generate_content or response.text access ***
        print(f"Error getting assessment from Gemini: {e}") # Log detailed error
        error_details = str(e)
        raw_resp_info = "Could not get raw response due to an early error."

        # Attempt to get more info if response object exists
        if response is not None:
            error_details = f"AI API error: {str(e)}"
            if hasattr(response, 'prompt_feedback'): # Check for prompt feedback attribute
                 print(f"Gemini API Prompt Feedback: {response.prompt_feedback}") # Log prompt feedback
                 error_details += f" | Prompt Feedback: {response.prompt_feedback}"
            if hasattr(response, 'text'): # Check if the response object has text
                 raw_resp_info = response.text

        return {"error": error_details, "raw_response": raw_resp_info} # Simplified error with details


def process_and_submit_essay(student_user_id, title, essay_content_html_param): # Renamed parameter
    if student_user_id is None:
         print(f"[{datetime.now()}] process_and_submit_essay called with student_user_id = None. This is unexpected.") # Debug print
         st.error("Could not save essay: User session issue. Please try logging out and in again.") # Simplified user error
         return
    if not title.strip():
        st.warning("Please add a title before submitting your essay.") # Keep this user feedback
        return

    essay_markdown = ""
    # Ensure content is not just empty HTML tags
    # *** USE THE PARAMETER NAME essay_content_html_param consistently ***
    if essay_content_html_param and essay_content_html_param != "<p><br></p>" and essay_content_html_param.strip() != "<p></p>":
        try:
            essay_markdown = md(essay_content_html_param)
        except Exception as e_md:
            print(f"Error converting essay content to Markdown: {e_md}") # Log error
            essay_markdown = "<i>Error converting content.</i>" # Basic fallback
            st.warning("Could not process essay formatting, submitting as plain text.") # User feedback
    else:
        st.warning("Essay content cannot be empty for submission.") # Keep this user feedback
        return

    with st.spinner("⏳ Evaluating and submitting your essay..."):
        # get_gemini_assessment is defined before this function
        ai_feedback_data = get_gemini_assessment(title, essay_markdown)
        print(f"[{datetime.now()}] DEBUG: Result from get_gemini_assessment: {ai_feedback_data}") # Debug print

    ai_feedback_json_str = None # Initialize to None
    overall_rating = None # Initialize to None

    if isinstance(ai_feedback_data, dict):
        print(f"[{datetime.now()}] DEBUG: AI feedback data is a dict.") # Debug print
        # Always attempt to dump the dictionary to JSON string
        try:
             ai_feedback_json_str = json.dumps(ai_feedback_data)
             print(f"[{datetime.now()}] DEBUG: Dumped AI feedback data to JSON string.") # Debug print
        except Exception as e:
             print(f"[{datetime.now()}] ERROR: Could not dump AI feedback dict to JSON string: {e}")
             ai_feedback_json_str = json.dumps({"error": f"Internal error dumping feedback: {e}"})
             st.error("⚠️ Internal error preparing AI feedback for saving.")


        if "error" not in ai_feedback_data:
            overall_rating = ai_feedback_data.get("overall_rating")
            print(f"[{datetime.now()}] DEBUG: AI feedback data has no 'error'. Overall rating: {overall_rating}") # Debug print
            st.success("🎉 Essay submitted and assessed successfully!")
            st.balloons()
        else:
            # AI returned an error dictionary
            print(f"[{datetime.now()}] DEBUG: AI feedback data contains 'error': {ai_feedback_data.get('error')}") # Debug print
            st.error("⚠️ There was an issue processing the AI feedback. Your essay was saved, but feedback may be missing.") # Simplified feedback error
            # ai_feedback_json_str is already set to the dumped error dictionary above
    else:
        # get_gemini_assessment did not return a dictionary (e.g., returned None unexpectedly)
        print(f"[{datetime.now()}] ERROR: get_gemini_assessment did not return a dictionary. Returned: {ai_feedback_data}") # Debug print
        ai_feedback_json_str = json.dumps({"error": "AI feedback data is not a valid structure or was None."})
        st.error("⚠️ An unexpected issue occurred with AI feedback. Your essay was saved, but feedback may be missing.")


    # Save the essay regardless of AI feedback success, if content and title are valid
    # save_essay_submission is now defined BEFORE this function
    # Removed the callable check as it seems to be part of the issue, relying on definition order
    print(f"[{datetime.now()}] DEBUG: Calling save_essay_submission...") # Debug print
    save_essay_submission(student_user_id, title, essay_markdown, ai_feedback_json_str, overall_rating)
    print(f"[{datetime.now()}] DEBUG: save_essay_submission called.") # Debug print


    # Reset state for next essay
    st.session_state.essay_started = False
    st.session_state.timer_start_time = None
    st.session_state.essay_title_input = ""
    st.session_state.essay_content_html = ""
    # Redirect to student dashboard to see past submissions
    st.session_state.view = 'student_dashboard'
    st.rerun() # Rerun to update the view


# --- Session State Initialization (for UI state variables) ---
# Added new views for clearer flow: 'student_profile', 'student_essay', 'student_dashboard'
if 'view' not in st.session_state: st.session_state.view = 'login'
if 'logged_in' not in st.session_state: st.session_state.logged_in = False
if 'user_type' not in st.session_state: st.session_state.user_type = None
if 'current_username' not in st.session_state: st.session_state.current_username = None
if 'current_user_id' not in st.session_state: st.session_state.current_user_id = None
if 'current_college_name' not in st.session_state: st.session_state.current_college_name = None
if 'essay_title_input' not in st.session_state: st.session_state.essay_title_input = ""
if 'essay_content_html' not in st.session_state: st.session_state.essay_content_html = ""
if 'essay_started' not in st.session_state: st.session_state.essay_started = False
if 'timer_start_time' not in st.session_state: st.session_state.timer_start_time = None
if 'submission_time_limit_seconds' not in st.session_state: st.session_state.submission_time_limit_seconds = 30 * 60
if 'profile_page_loaded' not in st.session_state: st.session_state.profile_page_loaded = False


# --- UI Sections ---
with st.sidebar:
    st.image(APP_LOGO_URL, width=180)
    st.title("AI Essay Grader")
    st.markdown("---")
    if st.session_state.logged_in:
        # Welcome message now uses the session state variable
        st.success(f"Welcome, **{st.session_state.get('current_username', 'User')}**!") # Use .get for safety just in case
        st.info(f"**Role:** {st.session_state.user_type.replace('_', ' ').title()}")
        if st.session_state.current_college_name:
            st.write(f"**College:** {st.session_state.current_college_name}")
        st.markdown("---")
        # Sidebar navigation for logged-in users
        if st.session_state.user_type == 'student':
             # Student navigation
             if st.button("👤 View Profile", use_container_width=True, key="nav_view_profile"): # Changed from "Edit Profile"
                 st.session_state.view = 'student_view_profile' # New view state
                 st.session_state.pop('profile_page_loaded', None) # Clear flag when navigating to view
                 st.rerun()
             if st.button("✍️ Start New Essay", use_container_width=True, key="nav_new_essay"):
                  st.session_state.view = 'student_essay'
                  # Reset essay state when navigating to start a new one
                  st.session_state.essay_started = False
                  st.session_state.timer_start_time = None
                  st.session_state.essay_title_input = ""
                  st.session_state.essay_content_html = ""
                  st.session_state.pop('profile_page_loaded', None) # Clear flag if navigating away from profile
                  st.rerun()
             if st.button("📚 View Past Submissions", use_container_width=True, key="nav_past_submissions"):
                  st.session_state.view = 'student_dashboard'
                  st.session_state.pop('profile_page_loaded', None) # Clear flag if navigating away from profile
                  st.rerun()
        elif st.session_state.user_type in ['college_admin', 'super_admin']:
            # Admin navigation
            if st.button("📊 View Reports", use_container_width=True, key="nav_view_reports"):
                  st.session_state.view = 'dashboard' # Admins share the main dashboard view
                  st.session_state.pop('profile_page_loaded', None) # Clear flag if navigating away from profile
                  st.rerun()
        # Super Admin specific nav
        if st.session_state.user_type == 'super_admin':
             if st.button("👑 Admin Management", use_container_width=True, key="nav_admin_management"):
                  st.session_state.view = 'super_admin_manage'
                  st.session_state.pop('profile_page_loaded', None) # Clear flag if navigating away from profile
                  st.rerun()

        st.markdown("---")
        if st.button("🚪 Logout", key="logout_button_sidebar", use_container_width=True, type="secondary"):
            logout() # logout() already clears profile_page_loaded
    else:
        st.info("Welcome! Please log in or sign up.")
        # Simplified sidebar buttons for login/signup
        if st.session_state.view == 'login':
            if st.button("✨ New Student? Sign Up", key="goto_signup_sidebar", use_container_width=True):
                st.session_state.view = 'signup'
                st.session_state.pop('profile_page_loaded', None) # Clear flag
                st.rerun()
        elif st.session_state.view == 'signup':
            if st.button("🔒 Already have an account? Login", key="goto_login_sidebar", use_container_width=True):
                st.session_state.view = 'login'
                st.session_state.pop('profile_page_loaded', None) # Clear flag
                st.rerun()
    st.markdown("---")
    st.caption("Powered by Truskill AI Technology")

# --- Main Content Area ---

if not st.session_state.logged_in:
    # Login/Signup UI remains similar, but directly tied to view state
    _, mid_col, _ = st.columns([0.5, 2, 0.5])
    with mid_col:
        st.markdown("<br><br>", unsafe_allow_html=True)
        if st.session_state.view == 'login':
            with st.container(border=True):
                st.header("🔐 User Login")
                with st.form("login_form_main"):
                    login_username = st.text_input("Username", key="login_user_main", placeholder="Enter your username")
                    login_password = st.text_input("Password", type="password", key="login_pass_main", placeholder="Enter your password")
                    st.markdown("<br>", unsafe_allow_html=True)
                    login_submitted = st.form_submit_button("🚀 Login", use_container_width=True, type="primary")
                    if login_submitted:
                        authenticate_user(login_username, login_password)
        elif st.session_state.view == 'signup':
             with st.container(border=True):
                st.header("👋 Student Sign Up")
                with st.form("signup_form_main"):
                    st.info("Create your student account to start.")
                    signup_username = st.text_input("Choose a Username", key="signup_uname_main", placeholder="e.g., Aniket Savardekar")
                    signup_password = st.text_input("Choose a Password", type="password", key="signup_pass_main", placeholder="Min. 6 characters")
                    signup_college_name = st.text_input("Enter Your College ID", key="signup_college_main", placeholder="Mention Your College Id ")
                    st.markdown("<br>", unsafe_allow_html=True)
                    signup_submitted = st.form_submit_button("📝 Create Account", use_container_width=True, type="primary")
                    if signup_submitted:
                        if signup_username and signup_password and signup_college_name:
                            if len(signup_password) < 6:
                                 st.warning("Password should be at least 6 characters long.")
                            else:
                                success, message = create_user(signup_username, signup_password, 'student', signup_college_name)
                                if success:
                                    st.success(message)
                                    st.session_state.view = 'login'
                                    st.rerun()
                                else: st.error(message)
                        else: st.warning("Please fill all fields.")

else: # User is logged in
    # Fetch profile info *at the start* of the logged-in section for potential use across student views
    # Admin views don't need profile info, but fetching here is safe
    student_profile = None
    # profile_incomplete is no longer used to gate the essay writing page

    if st.session_state.user_type == 'student' and st.session_state.current_user_id is not None:
         student_profile = get_student_profile(st.session_state.current_user_id)
         # We still calculate profile_incomplete for displaying warnings/info in the profile view
         profile_incomplete_for_display = True
         if student_profile is not None and isinstance(student_profile, dict):
             if student_profile.get('full_name', '').strip() and student_profile.get('department', '').strip():
                 profile_incomplete_for_display = False


    # Simplified Admin Views
    if st.session_state.user_type == 'super_admin':
        if st.session_state.view == 'super_admin_manage':
            st.header("👑 Super Admin: Manage Admins")
            st.markdown("Manage college administrator accounts.")
            st.markdown("---")
            with st.container(border=True):
                st.subheader("➕ Create College Admin Account")
                with st.form("create_college_admin_form_main"):
                    ca_username = st.text_input("College Admin Username", placeholder="e.g., cadmin_harvard")
                    ca_password = st.text_input("Set Temporary Password", type="password", placeholder="Min. 6 characters")
                    ca_college_name = st.text_input("College Name for this Admin", placeholder="e.g., Harvard University")
                    st.markdown("<br>", unsafe_allow_html=True)
                    submit_ca = st.form_submit_button("✅ Create College Admin", type="primary", use_container_width=True)
                    if submit_ca:
                        if ca_username and ca_password and ca_college_name:
                            if len(ca_password) < 6:
                                 st.warning("Password should be at least 6 characters.")
                            else:
                                success, message = create_user(ca_username, ca_password, 'college_admin', ca_college_name)
                                if success: st.success(message)
                                else: st.error(message)
                        else: st.warning("Please fill all fields.")
        # Super admin shares the 'dashboard' view for reports
        elif st.session_state.view == 'dashboard':
             st.header(f"👑 Super Admin: All College Reports")
             st.info("As a Super Admin, you can view reports across all colleges.")
             # Note: get_college_reports is currently filtered by college_name.
             # For Super Admin to see *all* reports, you would need a new function
             # like get_all_reports() that doesn't filter by college_name.
             st.warning("Reporting for Super Admin (viewing all colleges) is not fully implemented yet. Showing individual college report view.")
             # Fallback to the college admin view logic but with a placeholder
             st.subheader("📊 Student Essay Reports (College View Placeholder)") # Placeholder title
             st.caption("Filter and sorting apply to a single college's data for now.")
             # You would need to select a college or have a different report view here
             # For this simplified example, we'll just show an empty state or need a college selector
             st.info("Select a college to view reports (functionality pending).")


    elif st.session_state.user_type == 'college_admin':
        # College Admin always goes to dashboard view
        if st.session_state.view == 'dashboard':
            st.header(f"🎓 College Admin: {st.session_state.current_college_name}")
            st.subheader("📊 Student Essay Reports")
            # College Admin report view logic remains the same
            with st.container(border=True):
                st.markdown("#### Filter & Sort Options")
                cols_filter_sort1 = st.columns([1,1])
                with cols_filter_sort1[0]:
                    sort_by = st.selectbox("Sort by", options=["Submission Time", "Student Name", "Essay Title", "Overall Rating", "Department", "Roll Number"], index=0, key="college_sort_by")
                with cols_filter_sort1[1]:
                    sort_order_str = st.radio("Order", ["Descending", "Ascending"], index=0, horizontal=True, key="college_sort_order")
                    sort_ascending = True if sort_order_str == "Ascending" else False
                cols_filter_sort2 = st.columns([1,2])
                with cols_filter_sort2[0]:
                    filter_student_name = st.text_input("Filter by Student Name/Username", key="college_filter_name", placeholder="Type name...")
                with cols_filter_sort2[1]:
                    filter_rating_min, filter_rating_max = st.slider("Filter by Overall Rating", 0, 100, (0, 100), key="college_filter_rating")
                cols_date_filter = st.columns(2)
                with cols_date_filter[0]:
                    filter_date_start = st.date_input("Submissions From", value=None, key="college_filter_date_start")
                with cols_date_filter[1]:
                    filter_date_end = st.date_input("Submissions To", value=None, key="college_filter_date_end")
            st.markdown("---")
            all_reports_list = get_college_reports(st.session_state.current_college_name)
            if not all_reports_list:
                st.info(f"ℹ️ No student submissions found yet for {st.session_state.current_college_name}.")
            else:
                reports_df = pd.DataFrame(all_reports_list)
                if reports_df.empty:
                     st.info(f"ℹ️ No student submissions found yet for {st.session_state.current_college_name}.")
                else:
                    reports_df['submission_time_dt'] = pd.to_datetime(reports_df['submission_time'], errors='coerce')
                    reports_df['overall_rating'] = pd.to_numeric(reports_df['overall_rating'], errors='coerce').fillna(-1)
                    reports_df['student_department'] = reports_df['student_department'].astype(str).fillna('')
                    reports_df['student_roll_number'] = reports_df['student_roll_number'].astype(str).fillna('')
                    filtered_df = reports_df.copy()
                    if filter_student_name:
                        filtered_df['student_full_name'] = filtered_df['student_full_name'].astype(str).fillna('')
                        filtered_df['student_username'] = filtered_df['student_username'].astype(str).fillna('')
                        filtered_df = filtered_df[filtered_df['student_full_name'].str.contains(filter_student_name, case=False, na=False) | filtered_df['student_username'].str.contains(filter_student_name, case=False, na=False)]
                    filtered_df = filtered_df[(filtered_df['overall_rating'] >= filter_rating_min) & (filtered_df['overall_rating'] <= filter_rating_max)]
                    export_ready_df = filtered_df.copy()
                    if filter_date_start: export_ready_df = export_ready_df[export_ready_df['submission_time_dt'].notna() & (export_ready_df['submission_time_dt'].dt.date >= filter_date_start)]
                    if filter_date_end: export_ready_df = export_ready_df[export_ready_df['submission_time_dt'].notna() & (export_ready_df['submission_time_dt'].dt.date <= filter_date_end)]
                    sort_column_map = {"Submission Time": "submission_time_dt", "Student Name": "student_full_name", "Essay Title": "essay_title", "Overall Rating": "overall_rating", "Department": "student_department", "Roll Number": "student_roll_number"}
                    sort_col_actual = sort_column_map.get(sort_by, "submission_time_dt")
                    display_df = filtered_df.copy()
                    if sort_col_actual in ["student_full_name", "student_department", "student_roll_number", "essay_title"]:
                        display_df = display_df.sort_values(by=[sort_col_actual] + (['student_username'] if sort_col_actual == "student_full_name" else []), ascending=sort_ascending, na_position='last')
                        export_ready_df = export_ready_df.sort_values(by=[sort_col_actual] + (['student_username'] if sort_col_actual == "student_full_name" else []), ascending=sort_ascending, na_position='last')
                    else:
                         display_df = display_df.sort_values(by=sort_col_actual, ascending=sort_ascending)
                         export_ready_df = export_ready_df.sort_values(by=sort_col_actual, ascending=sort_ascending)
                    if not export_ready_df.empty:
                        with st.container(border=True):
                            st.markdown("#### 📄 Export Report")
                            export_data_list = []
                            for index, row in export_ready_df.iterrows():
                                export_row = {'Full Name': row.get('student_full_name', ''),'Department': row.get('student_department', ''),'Branch': row.get('student_branch', ''),'Roll Number': row.get('student_roll_number', ''),'Username': row.get('student_username', ''),'Essay Title': row.get('essay_title', ''),'Submission Datetime': row.get('submission_time', ''),'Overall Rating (0-100)': "Not Rated" if row.get('overall_rating', -1) == -1 else row.get('overall_rating')}
                                feedback_data_export = {}
                                ai_feedback_json_export = row.get('ai_feedback_json')
                                # --- FIX FOR COLLEGE ADMIN EXPORT ---
                                if ai_feedback_json_export:
                                    if isinstance(ai_feedback_json_export, dict):
                                        feedback_data_export = ai_feedback_json_export
                                    else:
                                        try:
                                            feedback_data_export = json.loads(ai_feedback_json_export)
                                        except (TypeError, json.JSONDecodeError):
                                            feedback_data_export = {} # Default to empty dict on any parsing error
                                    criteria_scores = feedback_data_export.get('criteria_scores', {})
                                    # --- END FIX ---
                                    if criteria_scores: # Ensure criteria_scores is not empty
                                        for crit, details in criteria_scores.items():
                                            crit_name_formatted = crit.replace('_', ' ').title() + " Score (0-10)"
                                            export_row[crit_name_formatted] = details.get('score', 'N/A')
                                export_data_list.append(export_row)
                            df_for_export = pd.DataFrame(export_data_list)
                            preferred_cols_order = ['Full Name', 'Department', 'Branch', 'Roll Number', 'Username', 'Essay Title', 'Submission Datetime', 'Overall Rating (0-100)']
                            existing_cols = df_for_export.columns.tolist()
                            final_export_cols_ordered = [col for col in preferred_cols_order if col in existing_cols]
                            for col in existing_cols:
                                if col not in final_export_cols_ordered: final_export_cols_ordered.append(col)
                            if final_export_cols_ordered: df_for_export = df_for_export[final_export_cols_ordered]
                            excel_buffer = io.BytesIO()
                            try:
                                with pd.ExcelWriter(excel_buffer, engine='openpyxl') as writer: df_for_export.to_excel(writer, index=False, sheet_name='Student Reports')
                                excel_buffer.seek(0)
                                college_name_safe = "".join(c if c.isalnum() else "_" for c in (st.session_state.current_college_name or "UnknownCollege"))
                                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                                excel_filename = f"student_reports_{college_name_safe}_{timestamp}.xlsx"
                                st.download_button(label="📥 Download Excel", data=excel_buffer, file_name=excel_filename, mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True, type="primary")
                            except Exception as e_excel: st.error(f"Error generating Excel file: {e_excel}")
                    elif not filtered_df.empty and (filter_date_start or filter_date_end): st.info("No reports match selected date period for export.")
                    st.markdown("---")
                    if display_df.empty: st.info("ℹ️ No reports match the current filter criteria.")
                    else:
                        st.markdown(f"**Displaying {len(display_df)} report(s):**")
                        for index, report_item_row in display_df.iterrows():
                            report_item = report_item_row.to_dict()
                            student_name = report_item.get('student_full_name', report_item.get('student_username', 'N/A'))
                            department = report_item.get('student_department', "N/A")
                            roll_number = report_item.get('student_roll_number', "N/A")
                            feedback_data = {}
                            ai_feedback_json = report_item.get('ai_feedback_json')
                            # --- FIX FOR COLLEGE ADMIN DISPLAY ---
                            if ai_feedback_json:
                                if isinstance(ai_feedback_json, dict):
                                    feedback_data = ai_feedback_json
                                else:
                                    try:
                                        feedback_data = json.loads(ai_feedback_json)
                                    except (TypeError, json.JSONDecodeError):
                                        feedback_data = {"error": "Could not parse feedback."}
                            else:
                                feedback_data = {"error": "Feedback data not available."}
                            # --- END FIX ---

                            rating_val = report_item.get('overall_rating', -1)
                            rating_display = "N/A" if rating_val == -1 else f"{rating_val:.0f}"
                            if isinstance(feedback_data, dict) and 'overall_rating' in feedback_data:
                                rating_from_feedback = feedback_data.get('overall_rating')
                                if isinstance(rating_from_feedback, (int, float)): rating_display = f"{rating_from_feedback:.0f}"
                                elif isinstance(rating, (int,float)): rating = f"{rating:.0f}"
                            elif isinstance(rating, (int,float)): rating = f"{rating:.0f}"

                            expander_title = f"📄 {student_name} (Roll: {roll_number}) - {report_item.get('essay_title', 'N/A')} (Rating: {rating_display})"
                            with st.expander(expander_title):
                                col_details1, col_details2 = st.columns([1,1])
                                with col_details1:
                                    st.markdown(f"**Full Name:** {student_name}")
                                    st.markdown(f"**Department:** {department}")
                                    st.markdown(f"**Essay Title:** {report_item.get('essay_title', 'N/A')}")
                                with col_details2:
                                    st.markdown(f"**Branch:** {report_item.get('student_branch', 'N/A')}")
                                    st.markdown(f"**Roll Number:** {report_item.get('student_roll_number', 'N/A')}")
                                    st.markdown(f"**Submitted:** {report_item.get('submission_time', 'N/A')}")
                                st.markdown(f"**Submitted Content:**\n```markdown\n{report_item.get('content_markdown', '')}\n```")
                                if feedback_data and not feedback_data.get("error"):
                                    st.markdown("**📝 AI Feedback:**")
                                    st.info(f"**Overall Rating:** {feedback_data.get('overall_rating', 'N/A')}/100 | **Word Count (AI):** {feedback_data.get('word_count', 'N/A')}")
                                    st.markdown(f"**Summary:** {feedback_data.get('overall_feedback', 'No summary.')}")
                                    criteria_scores_data = feedback_data.get('criteria_scores', {})
                                    if criteria_scores_data:
                                        st.markdown("##### Detailed Scores:")
                                        for criterion, details in criteria_scores_data.items():
                                            st.markdown(f"- **{criterion.replace('_', ' ').title()}:** {details.get('score', 'N/A')}/10 - *{details.get('justification', 'No justification.')}*")
                                else: st.warning("Feedback not available for this essay.")


    elif st.session_state.user_type == 'student':
        # Student flow: Login -> Essay -> Dashboard (Past Submissions) -> Profile (Optional Edit)
        # Fetch profile info *at the start* of the student section for the profile view and sidebar link
        student_profile = get_student_profile(st.session_state.current_user_id)

        # profile_incomplete is no longer used to gate the essay writing page.
        # We only need it for the warning message in the profile edit view.
        profile_incomplete_for_display = True
        if student_profile is not None and isinstance(student_profile, dict):
             if student_profile.get('full_name', '').strip() and student_profile.get('department', '').strip():
                 profile_incomplete_for_display = False

        # --- Manage profile_page_loaded flag to reset widget states ---
        if st.session_state.view == 'student_profile': # Edit Profile Form View
            if not st.session_state.get('profile_page_loaded', False):
                # Clear any old widget state for these inputs when entering the profile page
                profile_widget_keys = ['profile_full_name', 'profile_department', 'profile_branch', 'profile_roll', 'profile_email']
                for k in profile_widget_keys:
                    if k in st.session_state: # Check if key exists before popping
                        del st.session_state[k]
                st.session_state.profile_page_loaded = True
        elif st.session_state.view == 'student_view_profile': # New View Profile Page
             st.session_state.pop('profile_page_loaded', None) # Clear flag when entering view profile
        else:
            # If not on profile page or view profile page, ensure the flag is cleared for next visit
            if 'profile_page_loaded' in st.session_state:
                 del st.session_state.profile_page_loaded


        # --- Display the correct view based on st.session_state.view ---
        if st.session_state.view == 'student_view_profile':
             # --- Student View Profile (Read-Only) ---
             st.header(f"👤 Your Profile - {st.session_state.current_college_name}")
             st.markdown("---")
             # Fetch fresh profile for display
             current_profile_data = get_student_profile(st.session_state.current_user_id) or {}

             cols_profile_view = st.columns(2)
             with cols_profile_view[0]:
                 st.markdown(f"**Full Name:**")
                 st.markdown(f"### {current_profile_data.get('full_name','—')}")
                 st.markdown(f"**Department:**")
                 st.markdown(f"### {current_profile_data.get('department','—')}")
             with cols_profile_view[1]:
                 st.markdown(f"**Branch:**")
                 st.markdown(f"### {current_profile_data.get('branch','—')}")
                 st.markdown(f"**Roll Number:**")
                 st.markdown(f"### {current_profile_data.get('roll_number','—')}")

             st.markdown(f"**Email:**")
             st.markdown(f"### {current_profile_data.get('email','—')}")
             st.markdown("---")
             if st.button("✏️ Edit Profile", use_container_width=True, type="primary"):
                 st.session_state.view = 'student_profile' # Switch to edit mode
                 st.session_state.pop('profile_page_loaded', None) # Ensure edit form reloads defaults
                 st.rerun()


        elif st.session_state.view == 'student_profile': # This is now the Edit Profile Form
             # --- Student Profile Completion/Edit Form ---
             st.header(f"📝 Edit Profile - {st.session_state.current_college_name}")
             # Show warning if profile is incomplete (only in this view)
             if profile_incomplete_for_display:
                 st.warning("Please complete your profile details.")

             with st.container(border=True):
                 st.subheader("👤 Update Your Profile Details") # Update title to reflect editing capability
                 st.info("Fields marked with * are required.")
                 with st.form("profile_form_student"):
                    # Populate defaults if profile exists
                    s_full_name_default = student_profile.get('full_name', '') if student_profile and isinstance(student_profile, dict) else ""
                    s_department_default = student_profile.get('department', '') if student_profile and isinstance(student_profile, dict) else ""
                    s_branch_default = student_profile.get('branch', '') if student_profile and isinstance(student_profile, dict) else ""
                    s_roll_number_default = student_profile.get('roll_number', '') if student_profile and isinstance(student_profile, dict) else ""
                    s_email_default = student_profile.get('email', '') if student_profile and isinstance(student_profile, dict) else ""


                    s_full_name = st.text_input("Full Name*", value=s_full_name_default, key="profile_full_name", placeholder="Your full name")
                    s_department = st.text_input("Department*", value=s_department_default, key="profile_department", placeholder="e.g., Computer Science")
                    col1, col2 = st.columns([1,1])
                    with col1:
                        s_branch = st.text_input("Branch (Optional)", value=s_branch_default, key="profile_branch", placeholder="-")
                    with col2:
                        s_roll_number = st.text_input("Roll Number (Optional)", value=s_roll_number_default, key="profile_roll", placeholder="Your roll number")
                    s_email = st.text_input("Email (Optional)", value=s_email_default, key="profile_email", placeholder="your.email@example.com")
                    st.markdown("<br>", unsafe_allow_html=True)
                    submit_profile = st.form_submit_button("💾 Save Profile", use_container_width=True, type="primary")

                    if submit_profile:
                        if s_full_name and s_department:
                            if save_student_profile(st.session_state.current_user_id, s_full_name, s_department, s_branch, s_roll_number,s_email):
                                st.success("Profile saved successfully!")
                                # After saving, clear the loaded flag and switch to view profile mode
                                st.session_state.pop('profile_page_loaded', None)
                                st.session_state.view = 'student_view_profile' # Go back to view profile
                                st.rerun()

                        else:
                            st.warning("Please fill all required fields (Full Name, Department).")

        elif st.session_state.view == 'student_essay':
             # --- Student Essay Writing Section ---
             st.header(f"✍️ New Essay Test - {st.session_state.current_college_name}")

             # No longer block the essay page if profile is incomplete.
             # The warning will only be shown in the profile view.

             if not st.session_state.essay_started:
                 # Start New Essay section
                 with st.container(border=True):
                     st.subheader("Start Your Essay")
                     st.session_state.essay_title_input = st.text_input("Enter the title of your essay:", value=st.session_state.essay_title_input, key="essay_title_widget_main", placeholder="e.g., The Impact of Renewable Energy")
                     st.markdown("<br>", unsafe_allow_html=True)
                     if st.button("🚀 Start Writing My Essay!", disabled=(not st.session_state.essay_title_input.strip()), type="primary", use_container_width=True, key="start_essay_btn"):
                         if st.session_state.essay_title_input.strip():
                             st.session_state.essay_started = True
                             st.session_state.timer_start_time = time.time()
                             st.session_state.essay_content_html = "" # Clear content on start
                             st.rerun()
                         else: st.warning("Please enter an essay title.")

             elif st.session_state.essay_started:
                 # Essay writing in progress section
                 st.subheader(f"⏳ Writing: {st.session_state.essay_title_input}")
                 # ... (Timer calculation and display) ...
                 time_elapsed = time.time() - st.session_state.timer_start_time
                 time_remaining = st.session_state.submission_time_limit_seconds - time_elapsed
                 
                 col_timer, col_wc, col_submit = st.columns([2,1,1]) # Define columns

                 with col_timer:
                     if time_remaining > 0:
                         minutes = int(time_remaining // 60)
                         seconds = int(time_remaining % 60)
                         progress_value = time_elapsed / st.session_state.submission_time_limit_seconds
                         st.progress(progress_value, text=f"Time Left: {minutes:02d}:{seconds:02d}")
                         if time_remaining < 60: st.warning("Less than a minute remaining!")
                     else:
                         st.error("Time's Up!")

                 toolbar_config_essential = [['bold', 'italic', 'underline'], [{'header': 1}, {'header': 2}, {'header': 3}], [{'list': 'ordered'}, {'list': 'bullet'}], ['blockquote'], ['clean']]
                 st.caption("Use the toolbar below to format your essay.")

                 # --- Quill Editor is here ---
                 essay_html_content = st_quill(
                     value=st.session_state.get("essay_content_html", ""),
                     placeholder="Compose your brilliant essay here...",
                     html=True,
                     toolbar=toolbar_config_essential,
                     key="quill_editor_main",
                 )
                 st.session_state.essay_content_html = essay_html_content # Update session state

                 # *** WORD COUNT CALCULATION MOVED HERE ***
                 # The columns col_timer, col_wc, col_submit were defined above the timer display
                 with col_wc: # Use the previously defined word count column
                     temp_markdown_for_wc = md(essay_html_content) if essay_html_content and essay_html_content != "<p><br></p>" and essay_html_content.strip() != "<p></p>" else ""
                     word_count = calculate_word_count(temp_markdown_for_wc)
                     st.info(f"Words: **{word_count}**")
                 # *** END WORD COUNT CALCULATION ***


                 submit_button_placeholder = col_submit.empty() # Use the previously defined submit column

                 if time_remaining > 0:
                     if submit_button_placeholder.button("✅ Submit Essay", key="manual_submit_student_main", type="primary", use_container_width=True):
                         # process_and_submit_essay takes essay_html_content, so pass it
                         process_and_submit_essay(st.session_state.current_user_id, st.session_state.essay_title_input, essay_html_content)
                         # process_and_submit_essay handles rerunning and setting view to 'student_dashboard'

                     # Timer update logic - rerun every second only when writing
                     # Only rerun if time is still remaining and essay is started
                     if time_remaining > 0 and st.session_state.essay_started:
                          time.sleep(1)
                          st.rerun()
                 else:
                     # Auto-submit when time is up
                     submit_button_placeholder.empty()
                     if st.session_state.essay_started:
                         st.warning("Time's up! Submitting your essay automatically...")
                         # process_and_submit_essay takes essay_html_content, so pass it
                         process_and_submit_essay(st.session_state.current_user_id, st.session_state.essay_title_input, essay_html_content)
                         # process_and_submit_essay handles rerunning and setting view to 'student_dashboard'


        elif st.session_state.view == 'student_dashboard':
             # --- Student Past Submissions Dashboard ---
             st.header("📚 Your Past Submissions")
             # Display past essays...
             student_essays = get_student_essays(st.session_state.current_user_id)
             if not student_essays:
                 st.info("ℹ️ You haven't submitted any essays yet.")
                 # Optionally add a button to go to the essay writing page
                 if st.button("Start your first essay!"):
                     st.session_state.view = 'student_essay'
                     # Reset essay state when navigating to start a new one
                     st.session_state.essay_started = False
                     st.session_state.timer_start_time = None
                     st.session_state.essay_title_input = ""
                     st.session_state.essay_content_html = ""
                     st.session_state.pop('profile_page_loaded', None) # Clear flag
                     st.rerun()
             else:
                 st.markdown("---")
                 for essay_record in student_essays:
                     feedback_data = {}
                     ai_feedback_json = essay_record.get('ai_feedback_json')
                     if ai_feedback_json and isinstance(ai_feedback_json, (str, dict)): # Handle potential non-string JSON data
                         try:
                             # If it's already a dict (possible from JSONB fetch), use it directly
                             if isinstance(ai_feedback_json, dict):
                                 feedback_data = ai_feedback_json
                             else: # Otherwise, assume it's a JSON string
                                feedback_data = json.loads(ai_feedback_json)
                         except json.JSONDecodeError: feedback_data = {"error": "Could not parse feedback."}
                     else: # Handle case where ai_feedback_json is None or other unexpected type
                         feedback_data = {"error": "Feedback data not available."}


                     rating = essay_record.get('overall_rating') if essay_record.get('overall_rating') is not None else "N/A"
                     if isinstance(feedback_data, dict) and 'overall_rating' in feedback_data:
                         rating_from_fb = feedback_data.get('overall_rating')
                         if isinstance(rating_from_fb, (int, float)): rating = f"{rating_from_fb:.0f}"
                         elif isinstance(rating, (int,float)): rating = f"{rating:.0f}"
                     elif isinstance(rating, (int,float)): rating = f"{rating:.0f}"

                     # Format submission time for display
                     submission_time_display = essay_record.get('submission_time')
                     if isinstance(submission_time_display, datetime):
                         submission_time_display = submission_time_display.strftime('%Y-%m-%d %H:%M')
                     else:
                         submission_time_display = 'N/A'


                     expander_title_past = f"📜 {essay_record.get('title','N/A')} (Submitted: {submission_time_display}) - Rating: {rating}"
                     with st.expander(expander_title_past):
                         st.markdown(f"**Title:** {essay_record.get('title','N/A')}")
                         st.markdown(f"**Submitted Content (Markdown):**")
                         st.code(essay_record.get('content_markdown',''), language="markdown")

                         if feedback_data and not feedback_data.get("error"):
                             st.markdown("**📝 AI Feedback:**")
                             st.info(f"**Overall Rating:** {feedback_data.get('overall_rating', 'N/A')}/100 | **Word Count (AI):** {feedback_data.get('word_count', 'N/A')}")
                             st.markdown(f"**Summary:** {feedback_data.get('overall_feedback', 'No summary provided.')}")

                             criteria_scores_data = feedback_data.get('criteria_scores', {})
                             if criteria_scores_data:
                                 st.markdown("##### Detailed Scores (Text):")
                                 for criterion, details in criteria_scores_data.items():
                                      st.markdown(f"- **{criterion.replace('_', ' ').title()}:** {details.get('score', 'N/A')}/10 - *{details.get('justification', 'No justification.')}*")
                                 st.markdown("##### Detailed Scores (Graphical):")
                                 chart_data = {}
                                 for criterion, details in criteria_scores_data.items():
                                     criterion_name_formatted = criterion.replace('_', ' ').title()
                                     score_val = details.get('score', 0)
                                     if isinstance(score_val, (int, float)): chart_data[criterion_name_formatted] = score_val
                                     else: chart_data[criterion_name_formatted] = 0
                                 if chart_data:
                                     df_chart = pd.DataFrame(list(chart_data.items()), columns=['Criterion', 'Score'])
                                     df_chart = df_chart.set_index('Criterion')
                                     st.bar_chart(df_chart, height=300, use_container_width=True)
                                 else: st.caption("No numerical criteria scores available to plot.")
                             else: st.caption("No detailed criteria scores provided in feedback.")
                         else:
                             st.error(f"AI Feedback Error: {feedback_data.get('error', 'Unknown error')}")
                             if 'raw_response' in feedback_data:
                                 with st.expander("Show Raw AI Response"):
                                     st.text_area("Raw AI Response:", feedback_data['raw_response'], height=100, disabled=True)
                             st.warning("Feedback processing pending or not available.")

        else:
             # Fallback for unexpected view state for student
             st.error("An unexpected state occurred. Please try logging out and in again.")
             if st.button("Logout"):
                 logout()