import os
import re
import io
import base64
import random
from urllib.parse import urlparse, unquote
import traceback
from datetime import datetime, timezone # For story timestamps

from flask import Flask, render_template, request, redirect, url_for, flash, session, Response, make_response
import instaloader
import requests
from PIL import Image
from humanize import naturaltime # For relative timestamps (pip install humanize)


# --- Configuration ---
SECRET_KEY = os.environ.get('SECRET_KEY', 'local-dev-secret-fallback-value')
MAX_CAPTION_DISPLAY = 250
PREVIEW_SIZE = (200, 200)

# --- Initialize Flask App ---
app = Flask(__name__)
app.config['SECRET_KEY'] = SECRET_KEY

# --- Helper Functions ---

def get_shortcode_from_url(url):
    if not url: return None
    parsed = urlparse(url.strip())
    match = re.search(r"/(?:p|reel|reels|tv)/([a-zA-Z0-9_-]+)", parsed.path)
    return match.group(1) if match else None

def get_instaloader_instance():
    # Consider session reuse or login if needed for private content later
    return instaloader.Instaloader()

def fetch_preview_image(url, size=PREVIEW_SIZE):
    # (Keep existing function - no changes needed)
    if not url: return None, "No preview URL provided."
    try:
        response = requests.get(url, stream=True, timeout=10)
        response.raise_for_status()
        image_data = io.BytesIO(response.content)
        img = Image.open(image_data)
        if img.format == 'WEBP' and getattr(img, 'is_animated', False): img.seek(0)
        img.thumbnail(size)
        buffered = io.BytesIO()
        img_format = 'JPEG'
        if img.mode == 'RGBA' or img.mode == 'P': img = img.convert('RGB')
        img.save(buffered, format=img_format, quality=85)
        img_str = base64.b64encode(buffered.getvalue()).decode('utf-8')
        return f"data:image/{img_format.lower()};base64,{img_str}", None
    except requests.exceptions.RequestException as e: error = f"Network error fetching preview: {e}"; print(error); return None, error
    except Exception as e: error = f"Error processing preview image: {e}"; print(f"--- ERROR PROCESSING PREVIEW ---"); traceback.print_exc(); print(f"--- END PREVIEW ERROR ---"); return None, error

def generate_random_filename(media_url, is_video):
    # (Keep existing function - used for post items)
    random_num = random.randint(1, 9999); random_str = f"{random_num:04d}"
    extension = ".mp4" if is_video else ".jpg"
    try:
        parsed_url = urlparse(media_url); path = unquote(parsed_url.path)
        filename_part = os.path.basename(path).split('?')[0]
        _root, ext_from_url = os.path.splitext(filename_part)
        if ext_from_url and ext_from_url.lower() in ['.jpg', '.jpeg', '.png', '.webp', '.mp4', '.mov']:
            extension = ext_from_url.lower()
            if extension in ['.jpeg', '.png', '.webp']: extension = '.jpg'
    except Exception: pass
    return f"{random_str}{extension}"

# --- NEW Filename generator for stories ---
def generate_story_filename(username, index, media_url, is_video):
    """Generates a filename for stories (username_story_idx_random.ext)."""
    random_num = random.randint(1, 9999)
    ext = ".mp4" if is_video else ".jpg"
    try: # Try getting extension from URL
        parsed_url = urlparse(media_url); path = unquote(parsed_url.path)
        fn_part = os.path.basename(path).split('?')[0]
        _r, url_ext = os.path.splitext(fn_part)
        if url_ext and url_ext.lower() in ['.jpg', '.jpeg', '.png', '.webp', '.mp4', '.mov']:
            ext = url_ext.lower()
            if ext in ['.jpeg', '.png', '.webp']: ext = '.jpg'
    except Exception: pass
    # Sanitize username briefly
    safe_username = re.sub(r'[\\/*?:"<>|]', "", username)
    return f"{safe_username}_story_{index+1:02d}_{random_num:04d}{ext}"


# --- Flask Routes ---

@app.route('/', methods=['GET'])
def index():
    return render_template('index.html')

# --- MODIFIED Fetch Route ---
@app.route('/fetch', methods=['POST'])
def fetch_info():
    """Handles URL or Username submission, fetches info, shows results."""
    fetch_type = request.form.get('fetch_type') # 'url' or 'stories'

    L_fetch = get_instaloader_instance()
    result_data = {} # Data passed to template
    error_message = None
    session_key = None # Will hold the key used for session storage

    try:
        # --- BRANCH 1: Fetch by URL (Post/Reel) ---
        if fetch_type == 'url':
            url = request.form.get('url')
            shortcode = get_shortcode_from_url(url)
            if not shortcode:
                flash('Invalid Instagram Post/Reel URL format.', 'error')
                return redirect(url_for('index'))

            print(f"Fetching Post/Reel info for shortcode: {shortcode}")
            post = instaloader.Post.from_shortcode(L_fetch.context, shortcode)

            caption = post.caption if post.caption else "No caption"
            result_data = { # Data for template
                'shortcode': shortcode, 'username': post.owner_username,
                'likes': post.likes if post.likes else 'N/A', 'type': post.typename,
                'caption': (caption[:MAX_CAPTION_DISPLAY] + '...') if len(caption) > MAX_CAPTION_DISPLAY else caption,
                'full_caption': caption, 'media_items': []
            }
            session_media_items = [] # Lean data for session

            nodes_to_process = [post] if post.typename != 'GraphSidecar' else list(post.get_sidecar_nodes())

            for index, node in enumerate(nodes_to_process):
                is_video = node.is_video
                download_url = node.video_url if is_video else getattr(node, 'display_url', getattr(node, 'url', None))
                preview_url = getattr(node, 'display_url', getattr(node, 'url', None))
                if not download_url or not preview_url: continue

                preview_b64, preview_err = fetch_preview_image(preview_url)
                result_data['media_items'].append({'index': index, 'is_video': is_video, 'download_url': download_url, 'preview_b64': preview_b64, 'preview_error': preview_err})
                session_media_items.append({'index': index, 'is_video': is_video, 'download_url': download_url})

            # Store lean post data in session
            session_key = 'post_info_' + shortcode
            session[session_key] = {'shortcode': shortcode, 'username': result_data['username'], 'media_items': session_media_items}
            print(f"Stored lean post info for {shortcode} in session.")

        # --- BRANCH 2: Fetch by Username (Stories) ---
        elif fetch_type == 'stories':
            username = request.form.get('username', '').strip().lower()
            if not username:
                flash('Username cannot be empty.', 'error')
                return redirect(url_for('index'))

            print(f"Fetching Stories for username: {username}")
            profile = instaloader.Profile.from_username(L_fetch.context, username)

            result_data = { # Basic info for template
                'username': profile.username,
                'story_items': [] # Use 'story_items' key
            }
            session_story_items = [] # Lean data for session

            stories = L_fetch.get_stories(userids=[profile.userid])
            story_items_list = list(stories) # Consume the iterator

            if not story_items_list:
                flash(f"No active public stories found for user '{username}'.", 'info')
                return redirect(url_for('index'))

            print(f"Found {len(story_items_list)} story items for {username}.")
            now = datetime.now(timezone.utc) # For relative time calculation

            for index, item in enumerate(story_items_list):
                is_video = item.is_video
                download_url = item.video_url if is_video else item.url
                preview_url = item.url # StoryItem's .url is the preview
                if not download_url or not preview_url: continue

                preview_b64, preview_err = fetch_preview_image(preview_url)

                # Calculate relative time
                timestamp_utc = item.date_utc
                timestamp_relative = naturaltime(now - timestamp_utc) if timestamp_utc else "Unknown time"

                # Data for template (includes preview, timestamp)
                result_data['story_items'].append({
                    'index': index, 'is_video': is_video, 'download_url': download_url,
                    'preview_b64': preview_b64, 'preview_error': preview_err,
                    'timestamp_utc': timestamp_utc.isoformat() if timestamp_utc else None,
                    'timestamp_relative': timestamp_relative
                })
                # Lean data for session (only download essentials)
                session_story_items.append({'index': index, 'is_video': is_video, 'download_url': download_url})

            # Store lean story data in session, keyed by username
            session_key = 'story_info_' + username
            session[session_key] = {'username': username, 'story_items': session_story_items}
            print(f"Stored lean story info for {username} in session.")

        else:
            flash('Invalid fetch type specified.', 'error')
            return redirect(url_for('index'))

    # --- Common Exception Handling ---
    # (Keep existing exception blocks, ensure ProfileNotFoundError is included)
    except instaloader.exceptions.ProfileNotFoundError: error_message = f"Error: Instagram profile '{request.form.get('username', '') or shortcode}' not found."
    except instaloader.exceptions.PrivateProfileNotFollowedException: error_message = f"Error: Cannot access profile/post '{request.form.get('username', '') or shortcode}'. Profile is private."
    # ... (other instaloader exceptions) ...
    except instaloader.exceptions.InstaloaderException as e: error_message = f"Instaloader Error: {e}"
    except requests.exceptions.RequestException as e: error_message = f"Network Error during fetch: {e}"
    except Exception as e: error_message = f"An unexpected server error occurred during fetch."; print("--- UNEXPECTED fetch_info ERROR ---"); traceback.print_exc(); print("--- END TRACEBACK ---")

    if error_message:
        print(f"Error during fetch: {error_message}")
        flash(error_message, 'error')
        return redirect(url_for('index'))

    # --- Render Success ---
    if not result_data.get('media_items') and not result_data.get('story_items'):
         flash("Could not find any downloadable items.", 'error')
         return redirect(url_for('index'))

    # Render template with the combined result_data
    # The template uses {% if post.media_items %} or {% if post.story_items %}
    return render_template('results.html', post=result_data) # Use 'post' key consistently


# --- Download Route for Post Items ---
@app.route('/download_item/<shortcode>/<int:item_index>')
def download_item(shortcode, item_index):
    # (Keep existing function - no changes needed here, it uses 'post_info_' key)
    session_key = 'post_info_' + shortcode
    post_info_lean = session.get(session_key)
    if not post_info_lean: return make_response("Error: Download session expired. Please fetch the post info again.", 404)
    if item_index >= len(post_info_lean.get('media_items', [])): return make_response("Error: Invalid item requested.", 404)
    try:
        media_item = post_info_lean['media_items'][item_index]; media_url = media_item.get('download_url'); is_video = media_item.get('is_video', False)
    except (KeyError, IndexError) as e: print(f"Error accessing post session data: {e}"); return make_response("Error retrieving download details.", 500)
    if not media_url: return make_response("Error: Download URL not found.", 404)
    print(f"Initiating post download for item {item_index} from session URL: {media_url[:100]}...")
    try:
        response = requests.get(media_url, stream=True, timeout=60); response.raise_for_status()
        content_type = response.headers.get('Content-Type', None);
        if not content_type or 'octet-stream' in content_type: content_type = 'video/mp4' if is_video else 'image/jpeg'
        elif 'video' in content_type: is_video = True
        elif 'image' in content_type: is_video = False
        filename = generate_random_filename(media_url, is_video) # Use random name for posts
        download_response = Response(response.iter_content(chunk_size=65536), mimetype=content_type)
        download_response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
        content_length = response.headers.get('Content-Length');
        if content_length: download_response.headers['Content-Length'] = content_length
        print(f"Serving post file: {filename}, Type: {content_type}"); return download_response
    except requests.exceptions.Timeout: print(f"Timeout downloading post item {item_index}"); return make_response(f"Error: Request timed out.", 504)
    except requests.exceptions.RequestException as e: status_code = e.response.status_code if e.response is not None else 'N/A'; print(f"Network error downloading post item {item_index}: {e}"); return make_response(f"Error: Failed to retrieve file (Status: {status_code}).", status_code if isinstance(status_code, int) else 500)
    except Exception as e: print(f"--- UNEXPECTED post download error ---"); traceback.print_exc(); print(f"--- END TRACEBACK ---"); return make_response("Error: Server failed to prepare download.", 500)


# --- NEW Download Route for Story Items ---
@app.route('/download_story_item/<username>/<int:item_index>')
def download_story_item(username, item_index):
    """Fetches a specific story item and serves it for browser download."""
    session_key = 'story_info_' + username
    story_info_lean = session.get(session_key)

    # --- Validation ---
    if not story_info_lean:
        print(f"Session data not found for key: {session_key} during story download.")
        return make_response("Error: Story download session expired. Please fetch the username again.", 404)
    if item_index >= len(story_info_lean.get('story_items', [])):
        print(f"Invalid story item index {item_index} requested for {username}")
        return make_response("Error: Invalid story item requested.", 404)

    # --- Get required info from the LEAN session data ---
    try:
        media_item = story_info_lean['story_items'][item_index]
        media_url = media_item.get('download_url')
        is_video = media_item.get('is_video', False)
        # Get original username from session data for filename consistency
        original_username = story_info_lean.get('username', username)
    except (KeyError, IndexError) as e:
        print(f"Error accessing lean story session data for {username}, index {item_index}: {e}")
        return make_response("Error retrieving story download details from session.", 500)

    if not media_url:
        print(f"Missing story media_url for {username}, index {item_index} in session.")
        return make_response("Error: Story download URL not found.", 404)

    print(f"Initiating story download for item {item_index} from session URL: {media_url[:100]}...")

    # --- Fetching and Serving Logic ---
    try:
        response = requests.get(media_url, stream=True, timeout=60)
        response.raise_for_status()
        content_type = response.headers.get('Content-Type', None)
        if not content_type or 'octet-stream' in content_type:
            content_type = 'video/mp4' if is_video else 'image/jpeg'
        elif 'video' in content_type: is_video = True
        elif 'image' in content_type: is_video = False

        # Use the new story filename generator
        filename = generate_story_filename(original_username, item_index, media_url, is_video)

        download_response = Response(response.iter_content(chunk_size=65536), mimetype=content_type)
        download_response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
        content_length = response.headers.get('Content-Length')
        if content_length: download_response.headers['Content-Length'] = content_length

        print(f"Serving story file: {filename}, Type: {content_type}")
        return download_response

    # --- Error Handling during Download ---
    except requests.exceptions.Timeout: print(f"Timeout downloading story item {item_index}"); return make_response(f"Error: Request timed out.", 504)
    except requests.exceptions.RequestException as e: status_code = e.response.status_code if e.response is not None else 'N/A'; print(f"Network error downloading story item {item_index}: {e}"); return make_response(f"Error: Failed to retrieve story file (Status: {status_code}).", status_code if isinstance(status_code, int) else 500)
    except Exception as e: print(f"--- UNEXPECTED story download error ---"); traceback.print_exc(); print(f"--- END TRACEBACK ---"); return make_response("Error: Server failed to prepare story download.", 500)


# --- Run the App (for local development only) ---
if __name__ == '__main__':
    # (Keep existing local startup messages)
    print(f"\n * Flask Instagram Downloader (Local Development Mode)")
    if app.config['SECRET_KEY'] == 'local-dev-secret-fallback-value': print(" * WARNING: Using default local SECRET_KEY!")
    else: print(" * Secret Key: Set via ENV or Flask config")
    print(f" * Session storage: Default Client-Side")
    print(f" * Downloads will be prompted by the browser.")
    print(f" * Filenames will be random (posts) or username_story_idx_random (stories).")
    print(f"\n * Running on http://127.0.0.1:5001 or http://0.0.0.0:5001")
    print(" * Press CTRL+C to quit")
    app.run(debug=True, host='0.0.0.0', port=5001) # Remember Gunicorn runs 'app' directly in prod