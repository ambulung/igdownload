import os # Make sure os is imported
import re
import io
import base64
import random # For random filenames
from urllib.parse import urlparse, unquote
import traceback # For detailed error logging

from flask import Flask, render_template, request, redirect, url_for, flash, session, Response, make_response
import instaloader
import requests
from PIL import Image

# --- Configuration ---
# Use environment variable for SECRET_KEY, provide a default ONLY for local dev
SECRET_KEY = os.environ.get('SECRET_KEY', 'local-dev-secret-fallback-value')
MAX_CAPTION_DISPLAY = 250
PREVIEW_SIZE = (200, 200) # Size for preview thumbnails

# --- Helper Functions ---

def get_shortcode_from_url(url):
    """Extracts the shortcode from various Instagram URL formats."""
    if not url: return None
    parsed = urlparse(url.strip())
    # Regex to capture shortcode from /p/, /reel/, /reels/, /tv/ paths
    match = re.search(r"/(?:p|reel|reels|tv)/([a-zA-Z0-9_-]+)", parsed.path)
    return match.group(1) if match else None

def get_instaloader_instance():
    """Creates a fresh Instaloader instance."""
    # Can add configuration like user agents here if needed later
    # L = instaloader.Instaloader(user_agent="Mozilla/5.0 ...")
    return instaloader.Instaloader()

def fetch_preview_image(url, size=PREVIEW_SIZE):
    """Fetches an image URL, resizes, and returns its Base64 encoded version."""
    if not url: return None, "No preview URL provided."
    try:
        response = requests.get(url, stream=True, timeout=10)
        response.raise_for_status()
        image_data = io.BytesIO(response.content)
        img = Image.open(image_data)

        # Handle Animated WebP - take first frame
        if img.format == 'WEBP' and getattr(img, 'is_animated', False):
            img.seek(0)

        img.thumbnail(size) # Resize

        buffered = io.BytesIO()
        img_format = 'JPEG' # Convert previews to JPEG
        if img.mode == 'RGBA' or img.mode == 'P': # Handle transparency/palette
             img = img.convert('RGB')

        img.save(buffered, format=img_format, quality=85) # Save as JPEG
        img_str = base64.b64encode(buffered.getvalue()).decode('utf-8')
        return f"data:image/{img_format.lower()};base64,{img_str}", None

    except requests.exceptions.RequestException as e:
        error = f"Network error fetching preview: {e}"
        print(error)
        return None, error
    except Exception as e:
        error = f"Error processing preview image: {e}"
        print(f"--- ERROR PROCESSING PREVIEW from {url} ---")
        traceback.print_exc()
        print(f"--- END PREVIEW ERROR ---")
        return None, error


def generate_random_filename(media_url, is_video):
    """Generates a random 4-digit filename with correct extension."""
    random_num = random.randint(1, 9999)
    random_str = f"{random_num:04d}"
    # Default extensions
    extension = ".mp4" if is_video else ".jpg"

    # Try to get a better extension from URL
    try:
        parsed_url = urlparse(media_url)
        path = unquote(parsed_url.path)
        filename_part = os.path.basename(path).split('?')[0]
        _root, ext_from_url = os.path.splitext(filename_part)

        # Validate and use the extension if plausible
        if ext_from_url and ext_from_url.lower() in ['.jpg', '.jpeg', '.png', '.webp', '.mp4', '.mov']:
            extension = ext_from_url.lower()
            # Optional: Standardize common image types to .jpg
            if extension in ['.jpeg', '.png', '.webp']:
                 extension = '.jpg'
    except Exception:
        pass # Ignore errors, use the default

    return f"{random_str}{extension}"

# --- Flask Routes ---

@app.route('/', methods=['GET'])
def index():
    """Serves the main page with the input form."""
    return render_template('index.html')

@app.route('/fetch', methods=['POST'])
def fetch_info():
    """Handles URL submission, fetches info, shows results with download links."""
    url = request.form.get('url')
    shortcode = get_shortcode_from_url(url)

    if not shortcode:
        flash('Invalid Instagram URL format. Please provide a valid post/reel link.', 'error')
        return redirect(url_for('index'))

    L_fetch = get_instaloader_instance()
    post_info_for_template = {'shortcode': shortcode}
    error_message = None

    try:
        print(f"Fetching info for shortcode: {shortcode}")
        post = instaloader.Post.from_shortcode(L_fetch.context, shortcode)

        caption = post.caption if post.caption else "No caption"
        post_info_for_template.update({
            'username': post.owner_username,
            'likes': post.likes if post.likes else 'N/A',
            'type': post.typename,
            'caption': (caption[:MAX_CAPTION_DISPLAY] + '...') if len(caption) > MAX_CAPTION_DISPLAY else caption,
            'full_caption': caption,
            'media_items': []
        })

        media_nodes_to_process = []
        if post.typename == 'GraphSidecar':
            print(f"Processing GraphSidecar with {post.mediacount} items.")
            media_nodes_to_process = list(post.get_sidecar_nodes())
        else: # Single media post
            print("Processing single media post.")
            media_nodes_to_process.append(post)

        # Prepare list for template WITH previews
        # Prepare list for session WITHOUT previews
        session_media_items = []

        for index, node in enumerate(media_nodes_to_process):
            is_video = node.is_video
            download_url = node.video_url if is_video else getattr(node, 'display_url', getattr(node, 'url', None))
            preview_url = getattr(node, 'display_url', getattr(node, 'url', None))

            if not download_url or not preview_url:
                 print(f"Warning: Missing URL for item index {index}")
                 continue

            preview_b64, preview_err = fetch_preview_image(preview_url)

            # Add item with preview for the template
            post_info_for_template['media_items'].append({
                'index': index, 'is_video': is_video, 'download_url': download_url,
                'preview_b64': preview_b64, 'preview_error': preview_err
            })
            # Add item WITHOUT preview for the session
            session_media_items.append({
                'index': index, 'is_video': is_video, 'download_url': download_url
            })

            if preview_err: print(f"Warning: Failed to get preview for item {index}: {preview_err}")

        # Prepare the lean session data
        session_post_info = {
            'shortcode': post_info_for_template['shortcode'],
            'username': post_info_for_template['username'],
            'media_items': session_media_items # Use the list without previews
        }
        session_key = 'post_info_' + shortcode
        session[session_key] = session_post_info
        print(f"Stored lean info for {shortcode} in session (key: {session_key}).")

    # Exception Handling (keep same blocks as before)
    except instaloader.exceptions.ProfileNotFoundError: error_message = f"Error: Profile not found for post '{shortcode}'."
    except instaloader.exceptions.PrivateProfileNotFollowedException: error_message = f"Error: Cannot access '{shortcode}'. Profile is private."
    except instaloader.exceptions.LoginRequiredException: error_message = f"Error: Login required to access post '{shortcode}'."
    except instaloader.exceptions.PostNotFoundError: error_message = f"Error: Post '{shortcode}' not found."
    except instaloader.exceptions.QueryReturnedNotFoundException: error_message = f"Error: Post '{shortcode}' query returned not found."
    except instaloader.exceptions.ConnectionException as e: error_message = f"Error: Connection issue fetching '{shortcode}'. ({e})"
    except instaloader.exceptions.TooManyRequestsException: error_message = "Error: Too Many Requests from Instagram. Please wait."
    except instaloader.exceptions.InstaloaderException as e: error_message = f"Instaloader Error processing '{shortcode}': {e}"
    except requests.exceptions.RequestException as e: error_message = f"Network Error during preview fetch for '{shortcode}': {e}"
    except Exception as e:
        error_message = f"An unexpected server error occurred fetching info for '{shortcode}'."
        print(f"--- UNEXPECTED ERROR during fetch_info for {shortcode} ---"); traceback.print_exc(); print(f"--- END TRACEBACK ---")

    if error_message:
        print(f"Error preparing results for {shortcode}: {error_message}")
        flash(error_message, 'error')
        return redirect(url_for('index'))

    if not post_info_for_template['media_items']:
         flash(f"Could not find any downloadable media items for post '{shortcode}'.", 'error')
         return redirect(url_for('index'))

    # Render template with FULL info (including previews)
    return render_template('results.html', post=post_info_for_template)


# --- Download Route ---
@app.route('/download_item/<shortcode>/<int:item_index>')
def download_item(shortcode, item_index):
    """Fetches a specific media item and serves it for browser download."""

    session_key = 'post_info_' + shortcode
    post_info_lean = session.get(session_key)

    if not post_info_lean:
        print(f"Session data not found for key: {session_key} during download request.")
        return make_response("Error: Download session expired. Please go back and fetch the post info again.", 404)
    if item_index >= len(post_info_lean.get('media_items', [])):
         print(f"Invalid item index {item_index} requested for {shortcode}")
         return make_response("Error: Invalid item requested.", 404)

    try:
        media_item = post_info_lean['media_items'][item_index]
        media_url = media_item.get('download_url')
        is_video = media_item.get('is_video', False)
    except (KeyError, IndexError) as e:
         print(f"Error accessing lean session data for {shortcode}, index {item_index}: {e}")
         return make_response("Error retrieving download details from session.", 500)

    if not media_url:
        print(f"Missing media_url for {shortcode}, index {item_index} in session.")
        return make_response("Error: Download URL not found.", 404)

    print(f"Initiating browser download for item {item_index} from session URL: {media_url[:100]}...")

    try:
        response = requests.get(media_url, stream=True, timeout=60)
        response.raise_for_status()
        content_type = response.headers.get('Content-Type', None)
        if not content_type or 'octet-stream' in content_type:
            content_type = 'video/mp4' if is_video else 'image/jpeg'
        elif 'video' in content_type: is_video = True
        elif 'image' in content_type: is_video = False

        filename = generate_random_filename(media_url, is_video)
        download_response = Response(response.iter_content(chunk_size=65536), mimetype=content_type)
        download_response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
        content_length = response.headers.get('Content-Length')
        if content_length: download_response.headers['Content-Length'] = content_length

        print(f"Serving file: {filename}, Type: {content_type}, Size: {content_length or 'Unknown'}")
        return download_response

    except requests.exceptions.Timeout:
        error_msg = f"Error downloading item {item_index + 1}: The request timed out."
        print(error_msg)
        return make_response(f"Error: Request timed out.", 504)
    except requests.exceptions.RequestException as e:
        status_code = e.response.status_code if e.response is not None else 'N/A'
        error_msg = f"Failed to download item {item_index + 1} (Status: {status_code}). Network error or invalid/expired URL."
        print(f"Network error downloading item {item_index} from {media_url}: {e}")
        return make_response(f"Error: Failed to retrieve file (Status: {status_code}). URL may be invalid or expired.", status_code if isinstance(status_code, int) else 500)
    except Exception as e:
        error_msg = f"An server error occurred preparing item {item_index + 1} for download."
        print(f"--- UNEXPECTED error during download_item ({shortcode}/{item_index}) ---"); traceback.print_exc(); print(f"--- END TRACEBACK ---")
        return make_response("Error: Server failed to prepare download.", 500)


# --- Run the App (for local development only) ---
# Gunicorn will run the 'app' object directly in production
if __name__ == '__main__':
    print(f"\n * Flask Instagram Downloader (Local Development Mode)")
    # Check if using fallback secret key locally
    if app.config['SECRET_KEY'] == 'local-dev-secret-fallback-value':
        print(" * WARNING: Using default local SECRET_KEY!")
    else:
         print(" * Secret Key: Set via ENV or Flask config") # Or however you set it locally
    print(f" * Session storage: Default Client-Side")
    print(f" * Downloads will be prompted by the browser.")
    print(f" * Filenames will be random 4-digit numbers.")
    print(f"\n * Running on http://127.0.0.1:5001 or http://0.0.0.0:5001")
    print(" * Press CTRL+C to quit")

    # Use debug=False for more production-like local testing if desired
    # host='0.0.0.0' makes it accessible on your network
    app.run(debug=True, host='0.0.0.0', port=5001)