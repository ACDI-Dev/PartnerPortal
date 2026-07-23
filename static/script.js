(function(d, t) {
    var v = d.createElement(t), s = d.getElementsByTagName(t)[0];
    
    // 1. Get the query string from the current URL
    const queryString = window.location.search;

    // 2. Create a new URLSearchParams object
    const urlParams = new URLSearchParams(queryString);

    // 3. Extract the 'name' and 'email' properties
    const name = urlParams.get('name');
    const email = urlParams.get('email');
    
    v.onload = function() {
        window.voiceflow.chat.load({
            verify: { projectID: '69827fb16c43b800757e29c0' },
            url: 'https://general-runtime.voiceflow.com',
            versionID: 'production',
            
            // Render directly onto the page instead of a floating bubble
            render: {
                mode: 'embedded',
                target: document.getElementById('chat-container')
            },
            autostart: true, // Optional: forces the chat to begin without user prompt
            
            // 4. Pass the extracted variables into the launch payload
            launch: {
                event: {
                    type: 'launch',
                    payload: {
                        Name: name,
                        Email: email
                    }
                }
            },
            voice: {
                url: "https://runtime-api.voiceflow.com"
            }
        });
    }
    v.src = "https://cdn.voiceflow.com/widget-next/bundle.mjs"; 
    v.type = "text/javascript"; 
    s.parentNode.insertBefore(v, s);
})(document, 'script');