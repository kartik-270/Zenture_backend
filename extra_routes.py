
@api_bp.route('/appointments/<int:appointment_id>/status', methods=['PUT'])
@roles_required('counselor')
def update_appointment_status(appointment_id):
    data = request.json
    new_status = data.get('status')
    counselor_id = get_jwt_identity()

    if new_status not in ['booked', 'rejected', 'canceled']:
        return jsonify({"error": "Invalid status."}), 400

    appointment = Appointment.query.get(appointment_id)
    if not appointment:
        return jsonify({"error": "Appointment not found."}), 404

    if appointment.counselor_id != int(counselor_id):
        return jsonify({"error": "Unauthorized to update this appointment."}), 403

    appointment.status = new_status
    
    # Generate meeting link ONLY if accepted (booked) and it's a video call
    if new_status == 'booked' and appointment.mode == 'video_call' and not appointment.meeting_link:
        appointment.meeting_link = f"/session/{uuid.uuid4()}"
        
        # Notify Student
        msg = f"Your appointment has been accepted! Join via the video link."
        notif = Notification(user_id=appointment.student_id, message=msg, link=appointment.meeting_link)
        db.session.add(notif)
    elif new_status == 'rejected':
         msg = f"Your appointment request was declined."
         notif = Notification(user_id=appointment.student_id, message=msg, link=None)
         db.session.add(notif)

    db.session.commit()

    return jsonify({
        "message": f"Appointment {new_status} successfully.",
        "meeting_link": appointment.meeting_link
    })

@api_bp.route('/session/verify/<string:session_id>', methods=['GET'])
@jwt_required()
def verify_session_access(session_id):
    user_id = get_jwt_identity()
    user = User.query.get(user_id)
    
    # Check if a valid appointment exists with this link
    # The stored link is likely "/session/uuid", so we match that
    link_path = f"/session/{session_id}"
    
    appointment = Appointment.query.filter(
        Appointment.meeting_link == link_path,
        (Appointment.student_id == user_id) | (Appointment.counselor_id == user_id)
    ).first()
    
    if not appointment:
        return jsonify({"allowed": False, "error": "Invalid session or unauthorized."}), 403
        
    return jsonify({
        "allowed": True, 
        "user": {
            "id": user.id,
            "name": user.username,
            "role": user.role.value
        }
    })
