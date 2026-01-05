from gevent import monkey
monkey.patch_all() # Enables websocket to handle multiple connections effectively

from flask import Flask, request, jsonify, make_response,render_template
from flask import Blueprint

from flask_pymongo import PyMongo
from keycloak_auth import keycloak_protect, check_role
from mq import publish_event, start_consumer
import os
import uuid
import random

from flask_socketio import SocketIO,join_room, leave_room, emit
from datetime import datetime, timezone

blueprint = Blueprint('blueprint', __name__)

app = Flask(__name__)

# Load MongoDB URI
app.config["MONGO_URI"] = os.getenv("MONGO_URI")
if not app.config["MONGO_URI"]:
    raise RuntimeError("MONGO_URI not set")

mongo = PyMongo(app)
socketio = SocketIO(app, cors_allowed_origins="*", #Changing this to * for testing purposes
                    message_queue=os.getenv("REDIS_URL", None),
                    async_mode='gevent')

# ---------------------------
# SocketIO Events
# ---------------------------

@socketio.on('connect')
def on_connect(auth):
    token = auth.get('token') if auth else None
    print(f"Client connected to Motion Service with token: {token[:20] if token else 'None'}...")
    # Optionally validate token (Keycloak) here

@socketio.on('disconnect')
def on_disconnect():
    print("Client disconnected from Motion Service")

@socketio.on('join_motion_item')
def on_join_motion_item(data):
    motion_item_id = data.get('motion_item_id')
    if motion_item_id:
        join_room(motion_item_id)
        print(f"Client joined motion item room: {motion_item_id}")
        emit('status', {'msg': f'Joined motion item {motion_item_id}'}, room=motion_item_id)

@socketio.on('leave_motion_item')
def on_leave_motion_item(data):
    motion_item_id = data.get('motion_item_id')
    if motion_item_id:
        leave_room(motion_item_id)
        print(f"Client left motion item room: {motion_item_id}")
        emit('status', {'msg': f'Left motion item {motion_item_id}'}, room=motion_item_id)


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
    #if not motions: bc empty array is fine but python thinks it is falsy!!!!
    #    raise ValueError("Missing 'motions'")

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


def _set_poll_state(motion_item_id, state: str, extra: dict = None):
    """Helper to set poll subdocument state on a motion_item."""
    update = {"poll.poll_state": state}
    if extra:
        for k, v in extra.items():
            update[f"poll.{k}"] = v
    mongo.db.motion_items.update_one(
        {"motion_item_id": motion_item_id},
        {"$set": update}
    )

def _create_poll_for_motion(meeting_id: str, motion_item_id: str, motion_uuid: str, motion_text: str):
    """Create a poll for a specific motion with yes/no/abstain options."""
    options = ["yes", "no", "abstain"]
    poll_type = "single"
    
    # Build vote payload for VotingService
    vote_data = {
        "meeting_id": meeting_id,
        "pollType": poll_type,
        "options": options,
        "question": motion_text,  # Include the motion text as the poll question
        "origin": {
            "motion_item_id": motion_item_id,
            "motion_uuid": motion_uuid
        }
    }
    
    try:
        print(f"📤 Publishing voting.create event: {vote_data}")
        publish_event("voting.create", {"vote": vote_data})
        print(f"📊 Created poll for motion {motion_uuid}: {motion_text[:50]}...")
    except Exception as e:
        print(f"❌ Failed to create poll for motion {motion_uuid}: {e}")
        import traceback
        traceback.print_exc()
        raise


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

    uid = to_uuid(motion_item_id)
    if not uid:
        return jsonify({"error": "Invalid UUID"}), 400

    # Check if motion item exists
    existing = mongo.db.motion_items.find_one({"motion_item_id": uid})
    if not existing: 
        return jsonify({"error": "Motion item not found"}), 404

    # Check role against the meeting id from the motion item
    meeting_id = existing.get("meeting_id")
    if not check_role(request.user, meeting_id, "view"):
        return jsonify({"error": "Forbidden"}), 403
    
    data = request.get_json()
    if not data: 
        return jsonify({"error": "Missing request body"}), 400
    
    motion_text = data.get("motion")
    if not motion_text: 
        return jsonify({"error": "Missing 'motion'"}), 400

    motion = {
        "owner": user_id,
        "motion":  motion_text
    }
    
    try:
        add_motion_to_motion_item(uid, motion)
        
        # Get the newly created motion with its UUID
        updated_item = mongo.db.motion_items.find_one(
            {"motion_item_id": uid},
            {"_id": 0, "motions": 1}
        )
        if updated_item and updated_item.get("motions"):
            # Find the latest motion (the one we just added)
            new_motion = updated_item["motions"][-1]
            
            # Emit real-time event to all clients in this motion item room
            socketio.emit('motion_added', {
                'motion_item_id': uid,
                'motion': new_motion
            }, room=uid)
            
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

    # Check role against the meeting id from the motion item
    meeting_id = motion_item.get("meeting_id")
    if not check_role(request.user, meeting_id, "view"):
        return jsonify({"error": "Forbidden"}), 403

    # Find the specific motion and check ownership
    motion_owner = None
    for motion in motion_item.get("motions", []):
        if motion.get("motion_uuid") == motion_uuid:
            motion_owner = motion.get("owner")
            break
    
    if motion_owner != user_id:
        return jsonify({"error": "Only the motion owner can modify this motion"}), 403

    try:
        modify_motion_text(uid, motion_uuid, motion_text)
        
        # Get the updated motion
        updated_item = mongo.db.motion_items.find_one(
            {"motion_item_id": uid, "motions.motion_uuid": motion_uuid},
            {"_id": 0, "motions.$": 1}
        )
        
        if updated_item and updated_item.get("motions"):
            updated_motion = updated_item["motions"][0]
            
            # Emit real-time event to all clients in this motion item room
            socketio.emit('motion_updated', {
                'motion_item_id': uid,
                'motion': updated_motion
            }, room=uid)
            
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
        print(f"📥 Received motion.create_motion_item event: {data}")
        try:
            create_motion_item(data)
        except Exception as e:
            print(f"❌ Failed to create motion item: {e}")
            raise e
    if et == "motion.start_voting":
        # data = { meeting_id: uuid, motion_item_id: uuid }
        # Creates sequential polls for each motion, one at a time
        meeting_id = data.get("meeting_id")
        motion_item_id = data.get("motion_item_id")

        if not meeting_id or not motion_item_id:
            print("❌ Missing meeting_id or motion_item_id")
            return

        # Get all motions for this motion item
        motion_item = mongo.db.motion_items.find_one({"motion_item_id": motion_item_id})
        if not motion_item:
            print(f"❌ Motion item {motion_item_id} not found")
            return
        
        motions = motion_item.get("motions", [])
        if not motions:
            print(f"❌ No motions found for motion item {motion_item_id}")
            return
        
        # Initialize voting session tracking all motions
        voting_session = {
            "state": "in_progress",
            "motion_queue": [m["motion_uuid"] for m in motions],
            "current_index": 0,
            "poll_history": [],
            "started_at": datetime.now(timezone.utc).isoformat()
        }
        
        mongo.db.motion_items.update_one(
            {"motion_item_id": motion_item_id},
            {"$set": {"voting_session": voting_session}}
        )
        
        # Create poll for the first motion
        first_motion = motions[0]
        try:
            _create_poll_for_motion(
                meeting_id,
                motion_item_id,
                first_motion["motion_uuid"],
                first_motion["motion"]
            )
            print(f"✅ Started voting session for motion item {motion_item_id} with {len(motions)} motions")
        except Exception as e:
            print(f"❌ Failed to start voting session: {e}")
            mongo.db.motion_items.update_one(
                {"motion_item_id": motion_item_id},
                {"$set": {"voting_session.state": "error"}}
            )

    if et == "voting.created":
        # data: { poll_uuid, poll_id?, meeting_id, origin? }
        print(f"📥 Received voting.created event: {data}")
        poll_uuid = data.get("poll_uuid")
        origin = data.get("origin") or {}
        motion_item_id = origin.get("motion_item_id")
        motion_uuid = origin.get("motion_uuid")
        
        if motion_item_id and poll_uuid and motion_uuid:
            # Update current poll info
            poll_doc = {
                "poll_uuid": poll_uuid,
                "poll_state": "open",
                "motion_uuid": motion_uuid,
                "created_at": datetime.now(timezone.utc).isoformat()
            }
            
            mongo.db.motion_items.update_one(
                {"motion_item_id": motion_item_id},
                {"$set": {"poll": poll_doc}}
            )
            
            # Notify clients that a new poll is active
            socketio.emit("poll_started", {
                "motion_item_id": motion_item_id,
                "poll_uuid": poll_uuid,
                "motion_uuid": motion_uuid
            }, room=motion_item_id)
            
            print(f"✅ Poll {poll_uuid} opened for motion {motion_uuid}")
        else:
            print(f"⚠️ Incomplete voting.created data - motion_item_id: {motion_item_id}, poll_uuid: {poll_uuid}, motion_uuid: {motion_uuid}")

    if et == "voting.completed":
        # data: { poll_uuid, meeting_id, results, total_votes }
        poll_uuid = data.get("poll_uuid")
        results = data.get("results")
        total_votes = data.get("total_votes")
        
        if not poll_uuid:
            return
        
        # Find motion item by current poll_uuid
        motion_item = mongo.db.motion_items.find_one({"poll.poll_uuid": poll_uuid})
        if not motion_item:
            print(f"❌ Motion item not found for poll {poll_uuid}")
            return
        
        motion_item_id = motion_item["motion_item_id"]
        meeting_id = motion_item.get("meeting_id")
        voting_session = motion_item.get("voting_session")
        current_poll = motion_item.get("poll", {})
        motion_uuid = current_poll.get("motion_uuid")
        
        # Add to poll history
        if voting_session and motion_uuid:
            poll_record = {
                "motion_uuid": motion_uuid,
                "poll_uuid": poll_uuid,
                "results": results,
                "total_votes": total_votes,
                "completed_at": datetime.now(timezone.utc).isoformat()
            }
            
            mongo.db.motion_items.update_one(
                {"motion_item_id": motion_item_id},
                {
                    "$push": {"voting_session.poll_history": poll_record},
                    "$set": {"poll.poll_state": "completed"}
                }
            )
            
            # Notify clients that poll completed
            socketio.emit("poll_completed", {
                "motion_item_id": motion_item_id,
                "poll_uuid": poll_uuid,
                "motion_uuid": motion_uuid,
                "results": results,
                "total_votes": total_votes
            }, room=motion_item_id)
            
            print(f"✅ Poll {poll_uuid} completed for motion {motion_uuid}")
            
            # Check if there are more motions to vote on
            current_index = voting_session.get("current_index", 0)
            motion_queue = voting_session.get("motion_queue", [])
            next_index = current_index + 1
            
            if next_index < len(motion_queue):
                # Start next poll
                next_motion_uuid = motion_queue[next_index]
                
                # Find the motion text
                motions = motion_item.get("motions", [])
                next_motion = next((m for m in motions if m["motion_uuid"] == next_motion_uuid), None)
                
                if next_motion:
                    # Update index
                    mongo.db.motion_items.update_one(
                        {"motion_item_id": motion_item_id},
                        {"$set": {"voting_session.current_index": next_index}}
                    )
                    
                    # Create next poll
                    try:
                        _create_poll_for_motion(
                            meeting_id,
                            motion_item_id,
                            next_motion["motion_uuid"],
                            next_motion["motion"]
                        )
                        print(f"📊 Started poll {next_index + 1}/{len(motion_queue)} for motion {next_motion_uuid}")
                    except Exception as e:
                        print(f"❌ Failed to create next poll: {e}")
                        mongo.db.motion_items.update_one(
                            {"motion_item_id": motion_item_id},
                            {"$set": {"voting_session.state": "error"}}
                        )
                else:
                    print(f"❌ Next motion {next_motion_uuid} not found")
            else:
                # All motions have been voted on
                mongo.db.motion_items.update_one(
                    {"motion_item_id": motion_item_id},
                    {"$set": {
                        "voting_session.state": "completed",
                        "voting_session.completed_at": datetime.now(timezone.utc).isoformat()
                    }}
                )
                
                # Notify clients that all voting is complete
                socketio.emit("voting_session_completed", {
                    "motion_item_id": motion_item_id,
                    "total_polls": len(motion_queue),
                    "poll_history": voting_session.get("poll_history", []) + [poll_record]
                }, room=motion_item_id)
                
                print(f"✅ Voting session completed for motion item {motion_item_id}")

# Start consumer thread (after app exists)
default_bindings = os.getenv("MQ_BINDINGS", "motion.create_motion_item,motion.start_voting").split(",")
# Ensure we listen for voting lifecycle events
for rk in ["voting.created", "voting.completed"]:
    if rk not in default_bindings:
        default_bindings.append(rk)

start_consumer(
    queue=os.getenv("MQ_QUEUE", "motion-service"),
    bindings=default_bindings,
    on_event=on_event,
)

# Root health check (for Kubernetes)
@app.get("/")
def root():
    return "MotionService API running"

app.register_blueprint(blueprint)

if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 80))) 
