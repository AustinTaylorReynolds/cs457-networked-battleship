```mermaid
stateDiagram-v2
    [*] --> INIT
    INIT --> WAITING_FOR_PLAYERS : Server Started & Listening
    WAITING_FOR_PLAYERS --> GAME_START : 2 Clients Connected
    
    GAME_START --> PLAYER_TURN : Initialize 10x10 Boards & Assign Roles
    
    PLAYER_TURN --> EVALUATE_MOVE : Active Player Sends Valid MOVE
    PLAYER_TURN --> PLAYER_TURN : Invalid Move / Out-of-Turn (Send ERROR)
    
    EVALUATE_MOVE --> PLAYER_TURN : Hit / Miss (Next Player Turn)
    EVALUATE_MOVE --> GAME_OVER : All Ships Sunk (Victory Detected)
    
    WAITING_FOR_PLAYERS --> CLEANUP : Client Disconnects
    PLAYER_TURN --> GAME_OVER : Abrupt Disconnect / DISCONNECT (Win by Forfeit)
    EVALUATE_MOVE --> GAME_OVER : Abrupt Disconnect / DISCONNECT (Win by Forfeit)
    
    GAME_OVER --> CLEANUP : Broadcast Final Results
    CLEANUP --> WAITING_FOR_PLAYERS : Reset State