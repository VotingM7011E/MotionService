from gevent import monkey
monkey.patch_all() # Enables websocket to handle multiple connections effectively

from flask import Flask, request, jsonify, make_response,render_template
from flask import Blueprint

from flask_pymongo import PyMongo
from keycloak_auth import keycloak_protect, check_role
from mq import publish_event
import os
import uuid
import random

from flask_socketio import SocketIO,join_room, leave_room, emit

blueprint = Blueprint('blueprint', __name__)

app = Flask(__name__)

# Load MongoDB URI
app.config["MONGO_URI"] = os.getenv("MONGO_URI")
if not app.config["MONGO_URI"]:
    raise RuntimeError("MONGO_URI not set")

mongo = PyMongo(app)
#socketio = SocketIO(app, cors_allowed_origins="*", #Changing this to * for testing purposes
#                    message_queue=os.getenv("REDIS_URL", None),
#                    async_mode='gevent')

# ---------------------------
# SocketIO Events
# ---------------------------

#@socketio.on('connect')
#def on_connect(auth):
#   token = auth.get('token') if auth else None
    # validate token (Keycloak) and set user info (or disconnect)
#
#@socketio.on('join')
#def on_join(data):
#    meeting_id = data['meeting_id']
#    join_room(meeting_id)
#    emit('status', {'msg': f'User has entered the meeting {meeting_id}.'}, room=meeting_id)
#
#@socketio.on('leave')
#def on_leave(data):
#    meeting_id = data['meeting_id']
#    leave_room(meeting_id)
#    emit('status', {'msg': f'User has left the meeting {meeting_id}.'}, room=meeting_id)
#
#@socketio.on('Next Agenda Item')
#def moving_on_to_next_agenda_item(data):
#    emit('Next Agenda Item', data, room=data['meeting_id'])
#

def to_uuid(id_str):
    """Validate and convert UUID string."""
    try:
        return str(uuid.UUID(id_str))
    except Exception:
        return None


# ---------------------------
# Internal functions
# ---------------------------

def create_motion_item(data: dict):
    # Expects data on the form
    # data = {
    #   meeting_id: uuid
    #   motion_item_id: uuid
    #   motions: [
    #       {
    #           owner: username
    #           motion: string
    #       }
    #   ]
    # }

    meeting_id = data.get("meeting_id")
    motion_item_id = data.get("motion_item_id")
    motions = data.get("motions", [])

    if not meeting_id:
        raise ValueError("Missing 'meeting_id'")
    if not motion_item_id:
        raise ValueError("Missing 'motion_item_id'")
    if not motions:
        raise ValueError("Missing 'motions'")

    sanitized_motions = []

    for motion in motions:
        owner = motion.get("owner")
        motion_text = motion.get("motion")
        if not owner:
            raise ValueError(f"Missing 'owner' in {motion}")
        if not motion_text:
            raise ValueError(f"Missing 'motion_text' in {motion}")
        sanitized_motions.append({
            "owner": owner,
            "motion": motion_text,
            "motion_uuid": str(uuid.uuid4())
        })

    mongo.db.motion_items.insert_one({
        "meeting_id": meeting_id,
        "motion_item_id": motion_item_id,
        "motions": sanitized_motions
    })
    
def add_motion_to_motion_item(motion_item_id, motion):
    # Expects motion data on the form
    # motion = {
    #   owner: username
    #   motion: string
    # }
    # 

    if not motion_item_id: 
        raise ValueError("Missing 'motion_item_id'")
    if not motion: 
        raise ValueError("Missing 'motion'")

    owner = motion.get("owner")
    motion_text = motion.get("motion")

    if not owner: 
        raise ValueError(f"Missing 'owner' in {motion}")
    if not motion_text: 
        raise ValueError(f"Missing 'motion' in {motion}")

    sanitized_motion = {
        "owner": owner,
        "motion": motion_text,
        "motion_uuid": str(uuid.uuid4())
    }

    mongo.db.motion_items.update_one(
        {"motion_item_id": motion_item_id},
        {"$push": {"motions": sanitized_motion}}
    )

def modify_motion_text(motion_item_id, motion_uuid, motion_text):
    if not motion_item_id:
        raise ValueError("Missing 'motion_item_id'")
    if not motion_uuid: 
        raise ValueError("Missing 'motion_uuid'")
    if not motion_text:
        raise ValueError("Missing 'motion_text'")

    mongo.db.motion_items.update_one(
        {
            "motion_item_id": motion_item_id,
            "motions.motion_uuid": motion_uuid
        },
        {"$set": {"motions.$.motion":  motion_text}}
    )

def get_motion_item(motion_item_id):
    if not motion_item_id:
        raise ValueError("Missing 'motion_item_id'")
    mongo.db.meetings.find_one({"meeting_id": motion_item_id})


# ---------------------------
# Endpoints
# ---------------------------

# put this sippet ahead of all your bluprints
@blueprint.after_request 
def after_request(response):
    header = response.headers
    header['Access-Control-Allow-Origin'] = '*'
    header['Access-Control-Allow-Headers'] = "*"
    header['Access-Control-Allow-Methods'] = "*"
    # Other headers can be added here if needed
    return response


@blueprint.get("/items/<motion_item_id>/")
def get_motion_item_endpoint(motion_item_id):
    """
    GET /items/{motion_item_id}/
    Return motion item info. 
    """
    uid = to_uuid(motion_item_id)
    if not uid: 
        return jsonify({"error": "Invalid UUID"}), 400
    
    motion_item = mongo.db.motion_items.find_one(
        {"motion_item_id":  uid},
        {"_id": 0}  # Exclude MongoDB's _id from response
    )
    
    if not motion_item: 
        return jsonify({"error": "Motion item not found"}), 404

    return jsonify(motion_item), 200


@blueprint.post("/items/<motion_item_id>/motions")
@keycloak_protect
def add_motion_endpoint(motion_item_id):
    """
    POST /items/{motion_item_id}/motions
    Add a motion to the motion item. 
    """
    user_id = request.user["preferred_username"]
    if not user_id: 
        return jsonify({"error": "Unauthorized"}), 401

    if not check_role(request.user, poll.meeting_id, "view"):
        return jsonify({"error": "Forbidden"}), 403
    
    uid = to_uuid(motion_item_id)
    if not uid:
        return jsonify({"error": "Invalid UUID"}), 400
    
    data = request.get_json()
    if not data: 
        return jsonify({"error": "Missing request body"}), 400
    
    motion_text = data.get("motion")
    if not motion_text: 
        return jsonify({"error": "Missing 'motion'"}), 400

    # Check if motion item exists
    existing = mongo.db.motion_items.find_one({"motion_item_id": uid})
    if not existing: 
        return jsonify({"error": "Motion item not found"}), 404

    motion = {
        "owner": user_id,
        "motion":  motion_text
    }
    
    try:
        add_motion_to_motion_item(uid, motion)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    return jsonify({"message": "Motion added successfully"}), 201


@blueprint. get("/items/<motion_item_id>/motions")
def get_motions_endpoint(motion_item_id):
    """
    GET /items/{motion_item_id}/motions
    Get all motions for a motion item.
    """
    uid = to_uuid(motion_item_id)
    if not uid: 
        return jsonify({"error": "Invalid UUID"}), 400
    
    motion_item = mongo.db.motion_items.find_one(
        {"motion_item_id": uid},
        {"_id":  0, "motions": 1}
    )
    
    if not motion_item:
        return jsonify({"error": "Motion item not found"}), 404

    motions = motion_item.get("motions", [])
    return jsonify(motions), 200

@blueprint.patch("/items/<motion_item_id>/motions/<motion_id>")
@keycloak_protect
def patch_motion_endpoint(motion_item_id, motion_id):
    """
    PATCH /items/{motion_item_id}/motions/{motion_id}
    Change motion text. Only the motion owner can modify.
    """
    user_id = request.user["preferred_username"]
    if not user_id: 
        return jsonify({"error": "Unauthorized"}), 401

    if not check_role(request.user, poll.meeting_id, "view"):
        return jsonify({"error": "Forbidden"}), 403

    uid = to_uuid(motion_item_id)
    if not uid: 
        return jsonify({"error": "Invalid motion_item_id UUID"}), 400
    
    motion_uuid = to_uuid(motion_id)
    if not motion_uuid:
        return jsonify({"error": "Invalid motion_id UUID"}), 400
    
    data = request.get_json()
    if not data:
        return jsonify({"error": "Missing request body"}), 400
    
    motion_text = data.get("motion")
    if not motion_text: 
        return jsonify({"error": "Missing 'motion'"}), 400

    # Check if motion item and motion exist, and get owner
    motion_item = mongo.db.motion_items.find_one({
        "motion_item_id": uid,
        "motions.motion_uuid": motion_uuid
    })
    
    if not motion_item:
        return jsonify({"error": "Motion item or motion not found"}), 404

    # Find the specific motion and check ownership
    motion_owner = None
    for motion in motion_item.get("motions", []):
        if motion.get("motion_uuid") == motion_uuid:
            motion_owner = motion. get("owner")
            break
    
    if motion_owner != user_id:
        return jsonify({"error": "Only the motion owner can modify this motion"}), 403

    try:
        modify_motion_text(uid, motion_uuid, motion_text)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    return jsonify({"message": "Motion updated successfully"}), 200


# --- Inter-service -----------------

def on_event(event: dict):
    # event envelope: {event_type, data, ...}
    et = event.get("event_type")
    data = event.get("data", {})

    if et == "motion.create_motion_item":
        # Expects data on the form
        # data = {
        #   meeting_id: uuid
        #   motion_item_id: uuid
        #   motions: [
        #       {
        #           owner: username
        #           motion: string
        #       }
        #   ]
        # }
        create_motion_item(data)
    if et == "motion.start_voting":
        # data = {
        #   meeting_id: uuid
        #   motion_item_id: uuid
        # }

# Start consumer thread (after app exists)
start_consumer(
    queue=os.getenv("MQ_QUEUE", "motion-service"),
    bindings=os.getenv("MQ_BINDINGS", "motion.create_motion_item").split(","),
    on_event=on_event,
)

# Root health check (for Kubernetes)
@app.get("/")
def root():
    return "MotionService API running"

app.register_blueprint(blueprint)

if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 80))) 
