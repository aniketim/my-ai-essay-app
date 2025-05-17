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
try:
    GEMINI_API_KEY = st.secrets["GEMINI_API_KEY"]
except (FileNotFoundError, KeyError):
    # This fallback is for local development if secrets aren't set up exactly like on cloud
    # On Streamlit Cloud, the st.secrets["GEMINI_API_KEY"] should work directly
    GEMINI_API_KEY = "YOUR_GEMINI_API_KEY_HERE_IF_NOT_USING_ST_SECRETS_LOCALLY" 
    if GEMINI_API_KEY == "YOUR_GEMINI_API_KEY_HERE_IF_NOT_USING_ST_SECRETS_LOCALLY":
        st.error("🚨 Gemini API Key not found. Configure it in Streamlit Secrets for deployed app, or update fallback for local.")
        st.stop()

genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel(model_name='gemini-1.5-pro-latest')

# --- Database Connection Function (PostgreSQL) ---
def get_db_connection():
    try:
        db_url = st.secrets["DATABASE_URL"]
        conn = psycopg2.connect(db_url)
        return conn
    except Exception as e:
        st.error(f"Failed to connect to the database. Please check secrets. Error: {e}")
        # print(f"DB Connection Error: {e}") # For server logs
        return None

# --- Database Initialization Function (PostgreSQL) ---
def initialize_database_schema():
    print(f"[{datetime.now()}] Attempting to initialize PostgreSQL schema...")
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        if conn is None: return # Stop if connection failed

        cursor = conn.cursor()
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
                user_id INTEGER PRIMARY KEY,
                full_name TEXT NOT NULL,
                department TEXT NOT NULL,
                branch TEXT,
                roll_number TEXT,
                email TEXT,
                FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS essays (
                id SERIAL PRIMARY KEY,
                student_user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                content_markdown TEXT NOT NULL, 
                submission_time TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                ai_feedback_json JSONB, 
                overall_rating REAL,
                FOREIGN KEY (student_user_id) REFERENCES users (id) ON DELETE CASCADE
            )
        ''')
        conn.commit()
        print(f"[{datetime.now()}] PostgreSQL schema creation/check committed.")

        cursor.execute("SELECT id FROM users WHERE username = 'mainadmin'")
        if not cursor.fetchone():
            cursor.execute("INSERT INTO users (username, password_hash, user_type, college_name) VALUES (%s, %s, %s, %s)",
                           ('mainadmin', generate_password_hash('superpassword123'), 'super_admin', None))
            conn.commit()
            print(f"[{datetime.now()}] Default super_admin added to PostgreSQL.")
            
    except (Exception, psycopg2.Error) as error:
        print(f"PostgreSQL initialization error: {error}")
        # Optionally, inform the user in the UI, but for now, primarily log
        # st.error(f"Database setup error: {error}") 
    finally:
        if cursor: cursor.close()
        if conn: conn.close()
        print(f"[{datetime.now()}] PostgreSQL initialization routine finished.")

# --- Execute schema initialization (conditionally, once per app session/process) ---
# This ensures it runs when the app starts on Streamlit Cloud
if 'db_schema_initialized' not in st.session_state:
    initialize_database_schema()
    st.session_state.db_schema_initialized = True


# --- Authentication and User Data Functions (Using PostgreSQL) ---
def create_user(username, password, user_type, college_name=None):
    sql = "INSERT INTO users (username, password_hash, user_type, college_name) VALUES (%s, %s, %s, %s)"
    conn = None
    cursor = None
    try:
        conn = get_db_connection(); 
        if conn is None: return False, "Database connection failed."
        cursor = conn.cursor()
        cursor.execute(sql, (username, generate_password_hash(password), user_type, college_name))
        conn.commit()
        return True, "User created successfully."
    except (Exception, psycopg2.Error) as error:
        # Check for unique constraint violation (specific to username)
        if isinstance(error, psycopg2.IntegrityError) and "users_username_key" in str(error).lower():
             return False, "Username already exists."
        print(f"Error creating user: {error}")
        return False, f"An error occurred: {error}"
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def authenticate_user(username, password):
    conn = None
    cursor = None
    try:
        conn = get_db_connection(); 
        if conn is None: return
        cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cursor.execute("SELECT id, password_hash, user_type, college_name FROM users WHERE username = %s", (username,))
        user_record = cursor.fetchone()
        
        if user_record and check_password_hash(user_record['password_hash'], password):
            st.session_state.logged_in = True
            st.session_state.user_type = user_record['user_type']
            st.session_state.current_username = user_record['username']
            st.session_state.current_user_id = user_record['id'] 
            st.session_state.current_college_name = user_record['college_name']
            st.session_state.view = 'dashboard' 
            st.success(f"Logged in as {username} ({st.session_state.user_type})")
            st.rerun()
        else:
            st.error("Invalid username or password")
    except (Exception, psycopg2.Error) as error:
        st.error(f"Authentication error. Please try again.")
        print(f"Auth Error: {error}")
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def get_student_profile(user_id):
    conn = None
    cursor = None
    try:
        conn = get_db_connection(); 
        if conn is None: return None
        cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cursor.execute("SELECT full_name, department, branch, roll_number, email FROM student_profiles WHERE user_id = %s", (user_id,))
        profile = cursor.fetchone()
        return profile
    except (Exception, psycopg2.Error) as error:
        print(f"Error getting student profile: {error}")
        return None
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def save_student_profile(user_id, full_name, department, branch, roll_number, email):
    # Upsert logic for PostgreSQL
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
        if conn is None: return
        cursor = conn.cursor()
        cursor.execute(sql, (user_id, full_name, department, branch, roll_number, email))
        conn.commit()
    except (Exception, psycopg2.Error) as error:
        st.error("Failed to save profile.")
        print(f"Error saving student profile: {error}")
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def save_essay_submission(student_user_id, title, content_markdown, ai_feedback_json_str, overall_rating):
    sql = """
        INSERT INTO essays (student_user_id, title, content_markdown, submission_time, ai_feedback_json, overall_rating)
        VALUES (%s, %s, %s, %s, %s, %s)
    """ # submission_time will use default CURRENT_TIMESTAMP if not explicitly passed
    conn = None
    cursor = None
    submission_time_val = datetime.now() # Explicitly set for consistency
    try:
        conn = get_db_connection(); 
        if conn is None: return
        cursor = conn.cursor()
        # Pass None for overall_rating if it's truly not set or errored, DB column allows NULL
        db_overall_rating = overall_rating if isinstance(overall_rating, (int, float)) else None
        cursor.execute(sql, (student_user_id, title, content_markdown, submission_time_val, ai_feedback_json_str, db_overall_rating))
        conn.commit()
    except (Exception, psycopg2.Error) as error:
        st.error("Failed to save essay.")
        print(f"Error saving essay: {error}")
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def get_student_essays(student_user_id):
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
        essays = [dict(row) for row in cursor.fetchall()] # Convert to list of dicts
        return essays
    except (Exception, psycopg2.Error) as error:
        print(f"Error getting student essays: {error}")
        return []
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def get_college_reports(college_name): 
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
        WHERE u.college_name = %s
    '''
    try:
        conn = get_db_connection(); 
        if conn is None: return []
        cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cursor.execute(sql_query, (college_name,)) 
        reports_list = [dict(row) for row in cursor.fetchall()]
        return reports_list
    except (Exception, psycopg2.Error) as error:
        print(f"SQL Error in get_college_reports: {error}")
        return [] # Return empty list on error
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

# --- Helper Functions ---
def logout():
    keys_to_reset = ['logged_in', 'user_type', 'current_username', 'current_user_id', 
                     'current_college_name', 'essay_started', 'timer_start_time', 
                     'essay_title_input', 'essay_content_html', 'view'] 
    for key in keys_to_reset:
        if key in st.session_state:
            if key != 'db_schema_initialized': # Don't clear this one on logout
                del st.session_state[key] 
    st.session_state.logged_in = False
    st.session_state.view = 'login'
    st.success("Logged out.")
    st.rerun()

def calculate_word_count(text):
    return len(text.split()) if text else 0

# --- AI Logic ---
def get_gemini_assessment(title, essay_markdown):
    # ... (UNCHANGED - Gemini prompt and JSON parsing) ...
    prompt = f"""
    You are an AI assistant specialized in evaluating student essays.
    The essay title is: "{title}"
    The essay content (in Markdown) is:
    ---
    {essay_markdown}
    ---
    Please assess the essay based on the following criteria. For each criterion, provide a score from 0 to 10 (0 being very poor, 10 being excellent) and a brief justification.
    1.  Grammar and Mechanics: (e.g., spelling, punctuation, sentence structure errors)
    2.  Clarity and Cohesion: (e.g., logical flow, clear arguments, smooth transitions)
    3.  Content and Development: (e.g., depth of ideas, supporting evidence, originality, relevance to the title)
    4.  Sentence Formation and Variety: (e.g., complexity, conciseness, varied structures)
    5.  Formatting and Presentation (Markdown usage): (e.g., appropriate use of headings, lists, blockquotes if any, overall readability)

    After assessing individual criteria, also provide:
    -   Overall Word Count: The actual word count of the essay.
    -   Overall Feedback: A concise summary (2-3 sentences) of the essay's main strengths and areas for improvement.
    -   Overall Rating: A single numerical score from 0 to 100. This should be a weighted calculation based on the criteria scores. For example:
        - Grammar: 20%
        - Clarity and Cohesion: 25%
        - Content and Development: 30%
        - Sentence Formation: 15%
        - Formatting: 10%

    Output the entire response STRICTLY in the following JSON format. Do not include any text before or after the JSON object.
    Ensure all string values within the JSON are properly escaped.

    {{
      "criteria_scores": {{
        "grammar_and_mechanics": {{"score": <integer_0_to_10>, "justification": "<string_justification>"}},
        "clarity_and_cohesion": {{"score": <integer_0_to_10>, "justification": "<string_justification>"}},
        "content_and_development": {{"score": <integer_0_to_10>, "justification": "<string_justification>"}},
        "sentence_formation_and_variety": {{"score": <integer_0_to_10>, "justification": "<string_justification>"}},
        "formatting_and_presentation": {{"score": <integer_0_to_10>, "justification": "<string_justification>"}}
      }},
      "word_count": <integer_word_count>,
      "overall_feedback": "<string_summary_feedback>",
      "overall_rating": <integer_0_to_100_rating>
    }}
    """
    try:
        response = gemini_model.generate_content(prompt)
        response_text = response.text
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
            st.error(f"Could not find valid JSON in Gemini response. Raw response received:\n{response_text}")
            return {"error": "Invalid JSON structure from AI.", "raw_response": response_text}
    except json.JSONDecodeError as e:
        st.error(f"Error decoding JSON from AI: {e}. Raw response: {response.text}") 
        return {"error": f"JSON Decode Error: {e}", "raw_response": response.text}
    except Exception as e:
        st.error(f"Error getting assessment from Gemini: {e}")
        if hasattr(e, 'response') and hasattr(e.response, 'prompt_feedback'): 
            st.warning(f"Gemini API Prompt Feedback: {e.response.prompt_feedback}")
        return {"error": str(e)}

def process_and_submit_essay(student_user_id, title, essay_content_html):
    # ... (UNCHANGED - HTML to Markdown conversion, AI call, saving results using new save_essay_submission) ...
    if not title.strip():
        st.warning("Essay title cannot be empty for submission.")
        return
    essay_markdown = ""
    if essay_content_html and essay_content_html != "<p><br></p>": # Check if quill editor content is not empty
        try:
            essay_markdown = md(essay_content_html)
        except Exception as e_md:
            st.error(f"Error converting essay content to Markdown: {e_md}")
            essay_markdown = "<i>Error converting content - content was likely pure HTML.</i>" 
    else:
        st.warning("Essay content cannot be empty for submission.")
        return

    with st.spinner("⏳ Evaluating your essay with AI... Please wait."):
        ai_feedback_data = get_gemini_assessment(title, essay_markdown)

    ai_feedback_json_str = json.dumps(ai_feedback_data) 
    overall_rating = None 
    if isinstance(ai_feedback_data, dict) and "error" not in ai_feedback_data:
        overall_rating = ai_feedback_data.get("overall_rating") 
        st.success("🎉 Essay submitted and assessed successfully!")
        st.balloons()
    else:
        st.error("⚠️ There was an issue processing the AI feedback. The raw response may be available in the report if saved.")
    save_essay_submission(student_user_id, title, essay_markdown, ai_feedback_json_str, overall_rating)
    st.session_state.essay_started = False
    st.session_state.timer_start_time = None
    st.session_state.essay_title_input = ""
    st.session_state.essay_content_html = ""


# --- Session State Initialization (for UI state) ---
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
if 'submission_time_limit_seconds' not in st.session_state: st.session_state.submission_time_limit_seconds = 15 * 60


# --- UI Sections (Using the UI enhancements from the previous response) ---
with st.sidebar:
    st.image(APP_LOGO_URL, width=180) 
    st.title("AI Essay Grader") # Simplified title as logo is prominent
    st.markdown("---")
    if st.session_state.logged_in:
        st.success(f"Welcome, **{st.session_state.current_username}**!")
        st.info(f"**Role:** {st.session_state.user_type.replace('_', ' ').title()}")
        if st.session_state.current_college_name:
            st.write(f"**College:** {st.session_state.current_college_name}")
        st.markdown("---")
        if st.button("🚪 Logout", key="logout_button_sidebar", use_container_width=True, type="secondary"):
            logout()
    else:
        st.info("New Students, sign up Here.")
        if st.session_state.view == 'login':
            if st.button("✨ Don't have an account? Sign Up", key="goto_signup_sidebar", use_container_width=True):
                st.session_state.view = 'signup'
                st.rerun()
        elif st.session_state.view == 'signup':
            if st.button("🔒 Already have an account? Login", key="goto_login_sidebar", use_container_width=True):
                st.session_state.view = 'login'
                st.rerun()
    st.markdown("---")
    st.caption("Powered by Truskill AI Technology")

if not st.session_state.logged_in:
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
                    signup_college_name = st.text_input("Enter Your College Code", key="signup_college_main", placeholder="e.g., Ask College for College Code") 
                    st.markdown("<br>", unsafe_allow_html=True)
                    signup_submitted = st.form_submit_button("📝 Create Account", use_container_width=True, type="primary")
                    if signup_submitted:
                        if signup_username and signup_password and signup_college_name:
                            if len(signup_password) < 6:
                                st.warning("Password should be at least 6 characters long.")
                            else:
                                success, message = create_user(signup_username, signup_password, 'student', signup_college_name)
                                if success:
                                    st.success(message + " You can now log in.")
                                    st.session_state.view = 'login' 
                                    st.rerun() 
                                else: st.error(message)
                        else: st.warning("Please fill all fields.")
else: 
    if st.session_state.user_type == 'super_admin':
        st.header("👑 Super Admin Dashboard")
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

    elif st.session_state.user_type == 'college_admin':
        st.header(f"🎓 College Admin: {st.session_state.current_college_name}")
        st.subheader("📊 Student Essay Reports")
        
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
                            if ai_feedback_json_export:
                                try:
                                    feedback_data_export = json.loads(ai_feedback_json_export)
                                    criteria_scores = feedback_data_export.get('criteria_scores', {})
                                    for crit, details in criteria_scores.items():
                                        crit_name_formatted = crit.replace('_', ' ').title() + " Score (0-10)"
                                        export_row[crit_name_formatted] = details.get('score', 'N/A')
                                except json.JSONDecodeError: pass 
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
                        if ai_feedback_json:
                            try: feedback_data = json.loads(ai_feedback_json)
                            except json.JSONDecodeError: feedback_data = {"error": "Could not parse feedback."}
                        rating_val = report_item.get('overall_rating', -1) 
                        rating_display = "N/A" if rating_val == -1 else f"{rating_val:.0f}" 
                        if isinstance(feedback_data, dict) and 'overall_rating' in feedback_data:
                            rating_from_feedback = feedback_data.get('overall_rating')
                            if isinstance(rating_from_feedback, (int, float)): rating_display = f"{rating_from_feedback:.0f}"
                        expander_title = f"📄 {student_name} (Roll: {roll_number}) - {report_item.get('essay_title', 'N/A')} (Rating: {rating_display})"
                        with st.expander(expander_title):
                            col_details1, col_details2 = st.columns(2)
                            with col_details1:
                                st.markdown(f"**Full Name:** {student_name}")
                                st.markdown(f"**Department:** {department}")
                                st.markdown(f"**Essay Title:** {report_item.get('essay_title', 'N/A')}")
                            with col_details2:
                                st.markdown(f"**Branch:** {report_item.get('student_branch', 'N/A')}")
                                st.markdown(f"**Roll Number:** {roll_number}")
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
                            elif feedback_data.get("error"):
                                 st.error(f"Error in AI Feedback: {feedback_data.get('error')}")
                                 if 'raw_response' in feedback_data: st.text_area("Raw AI Response (for debugging):", feedback_data['raw_response'], height=100, disabled=True)
                            else: st.warning("Feedback not available for this essay.")
    
    elif st.session_state.user_type == 'student':
        st.header(f"📝 Student Dashboard - {st.session_state.current_college_name}")
        student_profile = get_student_profile(st.session_state.current_user_id) # This is now a DictRow or None
        
        profile_incomplete = True 
        if student_profile: # Check if profile exists
            # Access like a dictionary because of DictCursor
            if student_profile.get('full_name') and student_profile.get('department'):
                profile_incomplete = False
        
        if profile_incomplete: 
            with st.container(border=True):
                st.subheader("👤 Complete Your Profile")
                st.info("Please complete your profile to proceed. Fields marked with * are required.")
                with st.form("profile_form_student"):
                    s_full_name_default = student_profile['full_name'] if student_profile and student_profile.get('full_name') is not None else ""
                    s_department_default = student_profile['department'] if student_profile and student_profile.get('department') is not None else ""
                    s_branch_default = student_profile['branch'] if student_profile and student_profile.get('branch') is not None else ""
                    s_roll_number_default = student_profile['roll_number'] if student_profile and student_profile.get('roll_number') is not None else ""
                    s_email_default = student_profile['email'] if student_profile and student_profile.get('email') is not None else ""

                    s_full_name = st.text_input("Full Name*", value=s_full_name_default, placeholder="Your full name")
                    s_department = st.text_input("Department*", value=s_department_default, placeholder="e.g., Computer Science")
                    col1, col2 = st.columns(2)
                    with col1:
                        s_branch = st.text_input("Branch (Optional)", value=s_branch_default, placeholder="-")
                    with col2:
                        s_roll_number = st.text_input("Roll Number (Optional)", value=s_roll_number_default, placeholder="Your roll number")
                    s_email = st.text_input("Email (Optional)", value=s_email_default, placeholder="your.email@example.com")
                    st.markdown("<br>", unsafe_allow_html=True)
                    submit_profile = st.form_submit_button("💾 Save Profile", use_container_width=True, type="primary")
                    if submit_profile:
                        if s_full_name and s_department: 
                            save_student_profile(st.session_state.current_user_id, s_full_name, s_department, s_branch, s_roll_number,s_email)
                            st.success("Profile saved successfully!")
                            st.rerun()
                        else: st.warning("Please fill all required fields (Full Name, Department).")
        
        elif not st.session_state.essay_started:
            with st.container(border=True):
                st.subheader("✍️ Start New Essay Test")
                st.session_state.essay_title_input = st.text_input("Enter the title of your essay:", value=st.session_state.essay_title_input, key="essay_title_widget_main", placeholder="e.g., The Impact of Renewable Energy")
                st.markdown("<br>", unsafe_allow_html=True)
                if st.button("🚀 Start Writing My Essay!", disabled=(not st.session_state.essay_title_input.strip()), type="primary", use_container_width=True, key="start_essay_btn"):
                    if st.session_state.essay_title_input.strip():
                        st.session_state.essay_started = True
                        st.session_state.timer_start_time = time.time()
                        st.session_state.essay_content_html = "" 
                        st.rerun()
                    else: st.warning("Please enter an essay title.")

        elif st.session_state.essay_started:
            st.subheader(f"⏳ Writing: {st.session_state.essay_title_input}")
            time_elapsed = time.time() - st.session_state.timer_start_time
            time_remaining = st.session_state.submission_time_limit_seconds - time_elapsed
            
            toolbar_config_essential = [['bold', 'italic', 'underline'], [{'header': 1}, {'header': 2}, {'header': 3}], [{'list': 'ordered'}, {'list': 'bullet'}], ['blockquote'], ['clean']]
            st.caption("Use the toolbar below to format your essay.")
            essay_html_content = st_quill(
                value=st.session_state.get("essay_content_html", ""), 
                placeholder="Compose your brilliant essay here...", 
                html=True, 
                toolbar=toolbar_config_essential, 
                key="quill_editor_main",
                # height=400 # Consider adding height if you want it fixed
            )
            st.session_state.essay_content_html = essay_html_content

            col_timer, col_wc, col_submit = st.columns([2,1,1]) # Adjusted column ratios
            with col_timer:
                if time_remaining > 0:
                    minutes = int(time_remaining // 60)
                    seconds = int(time_remaining % 60)
                    progress_value = time_elapsed / st.session_state.submission_time_limit_seconds
                    st.progress(progress_value, text=f"Time Left: {minutes:02d}:{seconds:02d}")
                else:
                    st.error("Time's Up!")
            with col_wc:
                temp_markdown_for_wc = md(essay_html_content) if essay_html_content and essay_html_content != "<p><br></p>" else ""
                word_count = calculate_word_count(temp_markdown_for_wc)
                st.info(f"Words: **{word_count}**")
            
            submit_button_placeholder = col_submit.empty()

            if time_remaining > 0:
                if submit_button_placeholder.button("✅ Submit Essay", key="manual_submit_student_main", type="primary", use_container_width=True):
                    process_and_submit_essay(st.session_state.current_user_id, st.session_state.essay_title_input, essay_html_content)
                    st.rerun() 
                time.sleep(1) 
                st.rerun() 
            else:
                submit_button_placeholder.empty() 
                if st.session_state.essay_started: 
                    st.warning("Time's up! Submitting your essay automatically...")
                    process_and_submit_essay(st.session_state.current_user_id, st.session_state.essay_title_input, essay_html_content)
                    st.rerun() 
        
        student_essays = get_student_essays(st.session_state.current_user_id) # Returns list of dicts now
        if student_essays:
            st.markdown("---")
            st.subheader("📚 Your Past Submissions")
            for essay_record in student_essays: # Already a dict
                feedback_data = {}
                ai_feedback_json = essay_record.get('ai_feedback_json')
                if ai_feedback_json:
                    try: feedback_data = json.loads(ai_feedback_json)
                    except json.JSONDecodeError: feedback_data = {"error": "Could not parse feedback."}
                rating = essay_record.get('overall_rating') if essay_record.get('overall_rating') is not None else "N/A"
                if isinstance(feedback_data, dict) and 'overall_rating' in feedback_data: 
                    rating_from_fb = feedback_data.get('overall_rating')
                    if isinstance(rating_from_fb, (int, float)): rating = f"{rating_from_fb:.0f}"
                    elif isinstance(rating, (int,float)): rating = f"{rating:.0f}" 
                elif isinstance(rating, (int,float)): rating = f"{rating:.0f}"
                
                expander_title_past = f"📜 {essay_record.get('title','N/A')} (Submitted: {essay_record.get('submission_time','N/A')}) - Rating: {rating}"
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
                    elif feedback_data.get("error"):
                         st.error(f"Error in AI Feedback: {feedback_data.get('error')}")
                         if 'raw_response' in feedback_data: st.text_area("Raw AI Response (for debugging):", feedback_data['raw_response'], height=100, disabled=True)
                    else: st.warning("Feedback processing pending or not available.")