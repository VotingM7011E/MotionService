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
            "motion": motion_text
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
        "motion": motion_text
    }

    mongo.db.motion_items.update_one(
        {"motion_item_id": motion_item_id},
        {"$push": {"motions": sanitized_motion}}
    )




# ---------------------------
# Endpoints
# ---------------------------

# put this sippet ahead of all your bluprints
# blueprint can also be app~~
@blueprint.after_request 
def after_request(response):
    header = response.headers
    header['Access-Control-Allow-Origin'] = '*'
    header['Access-Control-Allow-Headers'] = "*"
    header['Access-Control-Allow-Methods'] = "*"
    # Other headers can be added here if needed
    return response





@blueprint.post("/meetings")
@keycloak_protect
def create_meeting():
    """
    POST /meetings
    Create a new meeting.
    """
    user_id = request.user["preferred_username"]
    if not user_id: 
        return jsonify({"error": "Unauthorized'"}), 401

    body = request.get_json()
    if not body or "meeting_name" not in body:
        return jsonify({"error": "meeting_name required"}), 400

    print(request.user)

    meeting_id = str(uuid.uuid4())
    meeting_code = generate_unique_meeting_code()

    mongo.db.meetings.insert_one({
        "meeting_id": meeting_id,
        "meeting_name": body["meeting_name"],
        "current_item": 0,
        "meeting_code": meeting_code
    })

    publish_event(
        routing_key="permission.create_meeting",
        data={
            "meeting_id": meeting_id,
            "creator_username": user_id
        }
    )

    created = {
        "meeting_id": meeting_id,
        "meeting_name": body["meeting_name"],
        "current_item": 0,
        "items": [],
        "meeting_code": meeting_code
    }

    return jsonify(created), 201

@blueprint.get("/meetings/<id>/")
def get_meeting(id):
    """
    GET /meetings/{id}/
    Return meeting info.
    """
    uid = to_uuid(id)
    if not uid:
        return jsonify({"error": "Invalid UUID"}), 400

    meeting = mongo.db.meetings.find_one({"meeting_id": uid})
    if not meeting:
        return jsonify({"error": "Meeting not found"}), 404

    agenda_items = list(mongo.db.agenda_items.find({"meeting_id": uid}))

    return jsonify(serialize_meeting(meeting, agenda_items)), 200

@blueprint.patch("/meetings/<meeting_id>")
@keycloak_protect
def update_meeting(meeting_id):
    """
    PATCH /meetings/{meeting_id}
    Update meeting fields such as current_item.
    """
    
    uid = to_uuid(meeting_id)
    if not uid:
        return jsonify({"error": "Invalid UUID"}), 400

    user_id = request.user["preferred_username"]
    if not user_id: 
        return jsonify({"error": "Unauthorized'"}), 401

    if not check_role(request.user, meeting_id, "manage"):
        return jsonify({"error": "Forbidden"}), 403
    
    meeting = mongo.db.meetings.find_one({"meeting_id": uid})
    if not meeting:
        return jsonify({"error": "Meeting not found"}), 404

    body = request.get_json()
    if not body:
        return jsonify({"error": "Request body required"}), 400

    update_fields = {}

    # Validate and update current_item
    if "current_item" in body:
        new_index = body["current_item"]

        if type(new_index) is not int or new_index < 0:
            return jsonify({"error": "current_item must be a non-negative integer"}), 400

        # Count agenda items
        item_count = mongo.db.agenda_items.count_documents({"meeting_id": uid})

        # Check if index is within valid range
        if new_index >= item_count:
            return jsonify({
                "error": "current_item is out of range",
                "max_valid_index": max(item_count - 1, 0),
                "agenda_items": item_count
            }), 400

        update_fields["current_item"] = new_index

    if not update_fields:
        return jsonify({"error": "No valid fields to update"}), 400

    # Apply patch
    mongo.db.meetings.update_one(
        {"meeting_id": uid},
        {"$set": update_fields}
    )

    # Return updated meeting
    updated_meeting = mongo.db.meetings.find_one({"meeting_id": uid})
    items = list(mongo.db.agenda_items.find({"meeting_id": uid}))

    socketio.emit('Next Agenda Item', {"meeting_id": uid, "current_item": new_index}, room=uid)
    socketio.emit('meeting_updated', serialize_meeting(updated_meeting, items), room=uid)

    return jsonify(serialize_meeting(updated_meeting, items)), 200

@blueprint.post("/meetings/<meeting_id>/agenda")
@keycloak_protect
def add_agenda_item(meeting_id):
    """
    POST /meetings/{meeting_id}/agenda
    Add an agenda item to meeting.
    """
    
    uid = to_uuid(meeting_id)
    if not uid:
        return jsonify({"error": "Invalid UUID"}), 400

    user_id = request.user["preferred_username"]
    if not user_id: 
        return jsonify({"error": "Unauthorized'"}), 401

    if not check_role(request.user, meeting_id, "manage"):
        return jsonify({"error": "Forbidden"}), 403

    meeting = mongo.db.meetings.find_one({"meeting_id": uid})
    if not meeting:
        return jsonify({"error": "Meeting not found"}), 404

    body = request.get_json()
    if not body or "item" not in body:
        return jsonify({"error": "item required"}), 400

    item = serialize_agenda_item(body["item"])
    if item is None:
        return jsonify({"error": "Invalid agenda item type"}), 400
    if isinstance(item, tuple):
        return item
    
    # Insert agenda item under meeting
    inserted = mongo.db.agenda_items.insert_one({
        "meeting_id": uid,
        **item
    })

    # Emit WebSocket event to notify clients
    socketio.emit('agenda_item_added', {"meeting_id": uid, "item": item}, room=uid)

    return jsonify({"message": "Agenda item added"}), 201

@blueprint.get("/meetings/<id>/agenda")
def get_agenda_items(id):
    """
    GET /meetings/{id}/agenda
    Returns all agenda items for the meeting.
    """
    uid = to_uuid(id)
    if not uid:
        return jsonify({"error": "Invalid UUID"}), 400

    meeting = mongo.db.meetings.find_one({"meeting_id": uid})
    if not meeting:
        return jsonify({"error": "Meeting not found"}), 404

    agenda_items = list(mongo.db.agenda_items.find({"meeting_id": uid}))

    return jsonify(agenda_items), 200

@blueprint.get("/code/<code>")
def get_meeting_id_from_code(code):
    """
    GET /code/{code}
    Returns the meeting UUID in plain text.
    """
    # Validate length & numeric
    if len(code) != 6 or not code.isdigit():
        return jsonify({"error": "Invalid meeting code format"}), 400

    meeting = mongo.db.meetings.find_one({"meeting_code": code})
    if not meeting:
        return "", 404

    return meeting["meeting_id"], 200

# Root health check (for Kubernetes)
@app.get("/")
def root():
    return "MotionService API running"

app.register_blueprint(blueprint)

if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 80))) 
